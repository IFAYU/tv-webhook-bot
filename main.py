from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
import json
import os

app = FastAPI()

# =========================
# 時區設定
# =========================
TW_TZ = timezone(timedelta(hours=8))

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def utc_to_tw_str(utc_str: Optional[str]) -> Optional[str]:
    if not utc_str:
        return None
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str

def parse_utc_to_tw_datetime(utc_str: str) -> datetime:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(TW_TZ)

# =========================
# 基本設定
# =========================
SECRET = os.getenv("SECRET", "abc123")
SIM_MODE = os.getenv("SIM_MODE", "true").lower() == "true"
LOG_FILE = os.getenv("LOG_FILE", "trade_log.jsonl")

# =========================
# 交易設定
# =========================
OPEN_QTY = int(os.getenv("OPEN_QTY", "2"))
MAX_QTY = int(os.getenv("MAX_QTY", "2"))   # 目前只做單次進場，不加碼

# =========================
# 風控設定
# =========================
BLOCK_DUPLICATE_ALERT = True
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-500.0"))
HARD_STOP_PER_CONTRACT = float(os.getenv("HARD_STOP_PER_CONTRACT", "100.0"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
ENABLE_STOP_LOSS_CHECK = True

# =========================
# 策略濾網設定
# =========================
ENABLE_CHOP_FILTER = True

# 張角至少要大於這個值，若 ATR 較大則用 ATR 動態放大
MIN_SPREAD_POINTS = float(os.getenv("MIN_SPREAD_POINTS", "50"))

# 動態張角倍率： max(MIN_SPREAD_POINTS, ATR * CHANNEL_ATR_MULT)
CHANNEL_ATR_MULT = float(os.getenv("CHANNEL_ATR_MULT", "1.2"))

# 距離預測牆最少要保留多少點空間
MIN_WALL_DISTANCE = float(os.getenv("MIN_WALL_DISTANCE", "20"))

# =========================
# 模擬帳戶
# =========================
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000000"))

# =========================
# 狀態
# =========================
def make_default_state():
    return {
        "mode": "SIM" if SIM_MODE else "LIVE",
        "has_long": False,
        "qty": 0,
        "avg_price": 0.0,
        "entry_time": None,
        "entry_time_tw": None,
        "entry_reason": None,
        "last_action_key": None,
        "last_signal_time": None,
        "last_signal_time_tw": None,
        "last_step": None,
        "initial_capital": INITIAL_CAPITAL,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "equity": INITIAL_CAPITAL,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown": 0.0,
        "last_price": 0.0,
        "consecutive_losses": 0,
        "trading_locked": False,
        "lock_reason": None,
        "position_history": [],
    }

state = make_default_state()

# =========================
# Payload
# =========================
class TradePayload(BaseModel):
    secret: str
    action: str          # long, close
    symbol: str
    timeframe: str
    strategy: str
    price: float
    time: str

    upper_rail: float    # 青藍上軌
    lower_rail: float    # 紅色下軌
    step_value: float    # 灰色階梯
    atr: float           # ATR
    nearest_wall: float  # 最近的絕對水平預測牆

def make_action_key(data: TradePayload) -> str:
    return f"{data.action}|{data.symbol}|{data.timeframe}|{data.time}"

def log_event(event: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def recalc_account_metrics(mark_price: float = 0.0):
    if mark_price > 0:
        state["last_price"] = round(mark_price, 4)

    if (
        state["has_long"]
        and state["qty"] > 0
        and state["avg_price"] > 0
        and state["last_price"] > 0
    ):
        state["unrealized_pnl"] = round(
            (state["last_price"] - state["avg_price"]) * state["qty"], 4
        )
    else:
        state["unrealized_pnl"] = 0.0

    state["equity"] = round(
        state["initial_capital"] + state["realized_pnl"] + state["unrealized_pnl"], 4
    )

    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]

    if state["peak_equity"] > 0:
        dd = state["peak_equity"] - state["equity"]
        if dd > state["max_drawdown"]:
            state["max_drawdown"] = round(dd, 4)

def lock_trading(reason: str):
    state["trading_locked"] = True
    state["lock_reason"] = reason

def check_risk_lock():
    if state["realized_pnl"] <= DAILY_LOSS_LIMIT:
        lock_trading(f"daily loss limit hit: {state['realized_pnl']}")
        return True

    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        lock_trading(f"max consecutive losses hit: {state['consecutive_losses']}")
        return True

    return state["trading_locked"]

def build_result(payload: TradePayload, note: str = "", extra: Optional[dict] = None) -> dict:
    received_utc = now_utc_iso()
    return {
        "received_at": received_utc,
        "received_at_tw": utc_to_tw_str(received_utc),
        "action": payload.action,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "strategy": payload.strategy,
        "price": payload.price,
        "time": payload.time,
        "time_tw": utc_to_tw_str(payload.time),
        "note": note,
        "extra": extra or {},
        "state_snapshot": {
            "mode": state["mode"],
            "has_long": state["has_long"],
            "qty": state["qty"],
            "avg_price": state["avg_price"],
            "entry_time": state["entry_time"],
            "entry_time_tw": state["entry_time_tw"],
            "entry_reason": state["entry_reason"],
            "last_price": state["last_price"],
            "realized_pnl": state["realized_pnl"],
            "unrealized_pnl": state["unrealized_pnl"],
            "equity": state["equity"],
            "peak_equity": state["peak_equity"],
            "max_drawdown": state["max_drawdown"],
            "consecutive_losses": state["consecutive_losses"],
            "trading_locked": state["trading_locked"],
            "lock_reason": state["lock_reason"],
            "last_signal_time": state["last_signal_time"],
            "last_signal_time_tw": state["last_signal_time_tw"],
            "last_step": state["last_step"],
        },
    }

# =========================
# 時段濾網
# =========================
def is_blocked_opening_window(tv_time: str) -> bool:
    """
    禁止 08:45 ~ 09:00 進場
    """
    try:
        dt_tw = parse_utc_to_tw_datetime(tv_time)
        total_minutes = dt_tw.hour * 60 + dt_tw.minute
        return (8 * 60 + 45) <= total_minutes < (9 * 60)
    except Exception:
        return False

# =========================
# 交易執行
# =========================
def execute_long(payload: TradePayload, entry_reason: str):
    if payload.price <= 0:
        raise ValueError("invalid entry price")

    if OPEN_QTY > MAX_QTY:
        raise ValueError("OPEN_QTY exceeds MAX_QTY")

    state["has_long"] = True
    state["qty"] = OPEN_QTY
    state["avg_price"] = round(payload.price, 4)
    state["entry_time"] = payload.time
    state["entry_time_tw"] = utc_to_tw_str(payload.time)
    state["entry_reason"] = entry_reason
    state["last_action_key"] = make_action_key(payload)
    state["last_signal_time"] = payload.time
    state["last_signal_time_tw"] = utc_to_tw_str(payload.time)

    recalc_account_metrics(payload.price)

    state["position_history"].append(
        {
            "action": "BUY_OPEN",
            "qty": OPEN_QTY,
            "price": round(payload.price, 4),
            "time": payload.time,
            "time_tw": utc_to_tw_str(payload.time),
            "reason": entry_reason,
        }
    )

def execute_close_all(payload: TradePayload, exit_reason: str):
    if not state["has_long"] or state["qty"] <= 0:
        raise ValueError("no long position to close")

    exit_qty = state["qty"]
    exit_avg = state["avg_price"]
    realized_pnl = round((payload.price - exit_avg) * exit_qty, 4)

    state["realized_pnl"] = round(state["realized_pnl"] + realized_pnl, 4)

    if realized_pnl < 0:
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    state["position_history"].append(
        {
            "action": "BUY_EXIT",
            "qty": exit_qty,
            "price": round(payload.price, 4),
            "time": payload.time,
            "time_tw": utc_to_tw_str(payload.time),
            "avg_price": round(exit_avg, 4),
            "realized_pnl": realized_pnl,
            "reason": exit_reason,
        }
    )

    state["has_long"] = False
    state["qty"] = 0
    state["avg_price"] = 0.0
    state["entry_time"] = None
    state["entry_time_tw"] = None
    state["entry_reason"] = None
    state["last_action_key"] = make_action_key(payload)
    state["last_signal_time"] = payload.time
    state["last_signal_time_tw"] = utc_to_tw_str(payload.time)

    recalc_account_metrics(payload.price)
    check_risk_lock()

    return realized_pnl

# =========================
# 核心濾網
# =========================
def check_long_filters(payload: TradePayload):
    reasons = []

    # 基本數值檢查
    if payload.price <= 0:
        reasons.append("invalid price")

    if payload.upper_rail <= 0 or payload.lower_rail <= 0:
        reasons.append("invalid rail values")

    if payload.atr <= 0:
        reasons.append("invalid atr")

    # 張角濾網
    spread = payload.upper_rail - payload.lower_rail
    min_spread = max(MIN_SPREAD_POINTS, payload.atr * CHANNEL_ATR_MULT)

    if ENABLE_CHOP_FILTER and spread < min_spread:
        reasons.append(
            f"盤整震盪，張角不足 spread={round(spread,4)} < min_spread={round(min_spread,4)}"
        )

    # 階梯濾網
    if state["last_step"] is not None and payload.step_value < state["last_step"]:
        reasons.append(
            f"階梯向下 step_value={round(payload.step_value,4)} < last_step={round(state['last_step'],4)}"
        )

    # 預測牆距離濾網
    wall_distance = abs(payload.nearest_wall - payload.price)
    if wall_distance < MIN_WALL_DISTANCE:
        reasons.append(
            f"距離預測牆太近 wall_distance={round(wall_distance,4)} < {MIN_WALL_DISTANCE}"
        )

    # 開盤前 15 分鐘不進
    if is_blocked_opening_window(payload.time):
        reasons.append("開盤前15分鐘不交易")

    return len(reasons) == 0, reasons, {
        "spread": round(spread, 4) if payload.upper_rail > 0 and payload.lower_rail > 0 else None,
        "min_spread": round(min_spread, 4) if payload.atr > 0 else None,
        "wall_distance": round(wall_distance, 4) if payload.nearest_wall > 0 else None,
    }

# =========================
# API
# =========================
@app.get("/")
async def root():
    return {"ok": True, "message": "tv-webhook-bot is running"}

@app.get("/health")
async def health():
    return {"ok": True, "state": state}

@app.get("/logs")
async def logs():
    if not os.path.exists(LOG_FILE):
        return {"ok": True, "count": 0, "logs": []}

    rows = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    return {"ok": True, "count": len(rows), "logs": rows[-50:]}

@app.post("/reset")
async def reset():
    global state
    state = make_default_state()
    return {"ok": True, "message": "state reset", "state": state}

@app.post("/webhook")
async def handle_webhook(payload: TradePayload):
    # =========================
    # 驗證
    # =========================
    if payload.secret != SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")

    recalc_account_metrics(payload.price)
    action_key = make_action_key(payload)

    # 同 K 同動作只允許一次
    same_bar_same_action = (
        state["last_signal_time"] == payload.time
        and state["last_action_key"] is not None
        and payload.action in state["last_action_key"]
    )
    if same_bar_same_action:
        result = build_result(payload, "same bar same action ignored")
        log_event(result)
        return {"ok": False, "reason": "same bar same action ignored", "result": result}

    # 防重複 alert
    if BLOCK_DUPLICATE_ALERT and state["last_action_key"] == action_key:
        result = build_result(payload, "duplicate alert ignored")
        log_event(result)
        return {"ok": False, "reason": "duplicate alert ignored", "result": result}

    # =========================
    # 若已有持倉，先檢查硬停損
    # =========================
    if ENABLE_STOP_LOSS_CHECK and state["has_long"] and state["qty"] > 0:
        stop_price = state["avg_price"] - HARD_STOP_PER_CONTRACT
        if payload.price <= stop_price:
            realized_pnl = execute_close_all(payload, "hard_stop_loss")
            state["last_step"] = payload.step_value

            result = build_result(
                payload,
                "SIM BUY_EXIT executed by hard stop",
                {"exit_reason": "hard_stop_loss", "realized_pnl": realized_pnl},
            )
            log_event(result)
            return {"ok": True, "message": "BUY_EXIT_HARD_STOP", "state": state, "result": result}

    # =========================
    # 鎖單檢查：只鎖進場，不鎖出場
    # =========================
    check_risk_lock()
    if state["trading_locked"] and payload.action == "long":
        state["last_step"] = payload.step_value
        result = build_result(payload, f"trading locked: {state['lock_reason']}")
        log_event(result)
        return {
            "ok": False,
            "reason": f"trading locked: {state['lock_reason']}",
            "result": result,
        }

    # =========================
    # 先處理已有持倉的出場
    # =========================
    if state["has_long"]:
        # 只要收到 close，就直接出
        if payload.action == "close":
            realized_pnl = execute_close_all(payload, "tv_close_signal")
            state["last_step"] = payload.step_value

            result = build_result(
                payload,
                "SIM BUY_EXIT executed by close signal",
                {"exit_reason": "tv_close_signal", "realized_pnl": realized_pnl},
            )
            log_event(result)
            return {"ok": True, "message": "BUY_EXIT", "state": state, "result": result}

        # 即使 TV 沒給 close，只要價格跌破下軌也出
        if payload.price < payload.lower_rail:
            realized_pnl = execute_close_all(payload, "price_below_lower_rail")
            state["last_step"] = payload.step_value

            result = build_result(
                payload,
                "SIM BUY_EXIT executed by lower rail break",
                {"exit_reason": "price_below_lower_rail", "realized_pnl": realized_pnl},
            )
            log_event(result)
            return {"ok": True, "message": "BUY_EXIT", "state": state, "result": result}

    # =========================
    # 沒有持倉才判斷進場
    # =========================
    if payload.action == "long":
        if state["has_long"]:
            state["last_step"] = payload.step_value
            result = build_result(payload, "already has long, long ignored")
            log_event(result)
            return {"ok": False, "reason": "already has long", "result": result}

        passed, reasons, metrics = check_long_filters(payload)

        if not passed:
            state["last_step"] = payload.step_value
            result = build_result(payload, "entry skipped by filters", {"reasons": reasons, "metrics": metrics})
            log_event(result)
            return {"ok": False, "reason": "entry skipped by filters", "result": result}

        try:
            execute_long(payload, "tv_long_signal_with_backend_filters")
        except Exception as e:
            state["last_step"] = payload.step_value
            result = build_result(payload, f"entry execution failed: {str(e)}")
            log_event(result)
            return {"ok": False, "reason": str(e), "result": result}

        state["last_step"] = payload.step_value
        result = build_result(
            payload,
            "SIM BUY_OPEN executed",
            {"entry_reason": "tv_long_signal_with_backend_filters", "metrics": metrics},
        )
        log_event(result)
        return {"ok": True, "message": "BUY_OPEN", "state": state, "result": result}

    # =========================
    # 沒有動作
    # =========================
    state["last_step"] = payload.step_value
    result = build_result(payload, "no action")
    log_event(result)
    return {"ok": False, "reason": "no action", "result": result}
