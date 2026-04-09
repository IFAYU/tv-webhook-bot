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

# =========================
# 基本設定
# =========================
SECRET = "abc123"
SIM_MODE = True
LOG_FILE = "trade_log.jsonl"

# 交易設定
OPEN_QTY = 2
MAX_QTY = 4

# 風控設定
BLOCK_DUPLICATE_ALERT = True
DAILY_LOSS_LIMIT = -500.0
HARD_STOP_PER_CONTRACT = 100.0
MAX_CONSECUTIVE_LOSSES = 3
ENABLE_STOP_LOSS_CHECK = True

# 模擬帳戶
INITIAL_CAPITAL = 1_000_000.0

# =========================
# 狀態
# =========================
state = {
    "mode": "SIM" if SIM_MODE else "LIVE",
    "has_long": False,
    "qty": 0,
    "avg_price": 0.0,
    "last_action_key": None,
    "last_signal_time": None,
    "last_signal_time_tw": None,
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

# =========================
# Payload
# =========================
class TVPayload(BaseModel):
    secret: str
    action: str
    symbol: str
    timeframe: str
    strategy: str
    price: Optional[str] = None
    time: Optional[str] = None

def make_action_key(data: TVPayload) -> str:
    return f"{data.action}|{data.symbol}|{data.timeframe}|{data.time}"

def safe_float(v: Optional[str]) -> float:
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0

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

def build_result(payload: TVPayload, note: str = "") -> dict:
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
        "time_tw": utc_to_tw_str(payload.time) if payload.time else None,
        "note": note,
        "state_snapshot": {
            "mode": state["mode"],
            "has_long": state["has_long"],
            "qty": state["qty"],
            "avg_price": state["avg_price"],
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
        },
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

    return {"ok": True, "count": len(rows), "logs": rows[-20:]}

@app.post("/reset")
async def reset():
    global state

    state = {
        "mode": "SIM" if SIM_MODE else "LIVE",
        "has_long": False,
        "qty": 0,
        "avg_price": 0.0,
        "last_action_key": None,
        "last_signal_time": None,
        "last_signal_time_tw": None,
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

    return {"ok": True, "message": "state reset", "state": state}

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload):
    if payload.secret != SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")

    action_key = make_action_key(payload)
    action = payload.action
    price = safe_float(payload.price)

    # 先更新市價 / 帳戶
    recalc_account_metrics(price)

    # 同K同動作只允許一次
    same_bar_same_action = (
        state["last_signal_time"] == payload.time
        and state["last_action_key"] is not None
        and payload.action in state["last_action_key"]
    )
    if same_bar_same_action:
        result = build_result(payload, "same bar same action ignored")
        log_event(result)
        return {
            "ok": False,
            "reason": "same bar same action ignored",
            "result": result,
        }

    # 若已有持倉，先做停損檢查
    if ENABLE_STOP_LOSS_CHECK and state["has_long"] and state["qty"] > 0:
        stop_price = state["avg_price"] - HARD_STOP_PER_CONTRACT
        if price > 0 and price <= stop_price:
            action = "BUY_EXIT"

    # 鎖單檢查：只鎖 BUY_OPEN，不鎖 BUY_EXIT
    check_risk_lock()
    if state["trading_locked"] and action == "BUY_OPEN":
        result = build_result(payload, f"trading locked: {state['lock_reason']}")
        log_event(result)
        return {
            "ok": False,
            "reason": f"trading locked: {state['lock_reason']}",
            "result": result,
        }

    # 防重複 alert
    if BLOCK_DUPLICATE_ALERT and state["last_action_key"] == action_key:
        result = build_result(payload, "duplicate alert ignored")
        log_event(result)
        return {
            "ok": False,
            "reason": "duplicate alert ignored",
            "result": result,
        }

    # -------------------------
    # BUY_OPEN
    # -------------------------
    if action == "BUY_OPEN":
        if state["has_long"]:
            result = build_result(payload, "already has long, BUY_OPEN ignored")
            log_event(result)
            return {
                "ok": False,
                "reason": "already has long",
                "result": result,
            }

        if price <= 0:
            result = build_result(payload, "invalid price, BUY_OPEN ignored")
            log_event(result)
            return {
                "ok": False,
                "reason": "invalid price",
                "result": result,
            }

        if OPEN_QTY > MAX_QTY:
            result = build_result(payload, "OPEN_QTY exceeds MAX_QTY")
            log_event(result)
            return {
                "ok": False,
                "reason": "OPEN_QTY exceeds MAX_QTY",
                "result": result,
            }

        state["has_long"] = True
        state["qty"] = OPEN_QTY
        state["avg_price"] = round(price, 4)
        state["last_action_key"] = action_key
        state["last_signal_time"] = payload.time
        state["last_signal_time_tw"] = utc_to_tw_str(payload.time)

        recalc_account_metrics(price)

        state["position_history"].append(
            {
                "action": "BUY_OPEN",
                "qty": OPEN_QTY,
                "price": round(price, 4),
                "time": payload.time,
                "time_tw": utc_to_tw_str(payload.time),
            }
        )

        result = build_result(payload, f"SIM BUY_OPEN executed, qty={OPEN_QTY}")
        log_event(result)
        return {
            "ok": True,
            "message": "BUY_OPEN received",
            "state": state,
            "result": result,
        }

   # -------------------------
    # BUY_ADD（停用）
    # -------------------------
    elif action == "BUY_ADD":
        result = build_result(payload, "BUY_ADD disabled")
        log_event(result)
        return {
            "ok": False,
            "reason": "BUY_ADD disabled",
            "result": result,
        }

    # -------------------------
    # BUY_EXIT
    # -------------------------
    elif action == "BUY_EXIT":
        if not state["has_long"] or state["qty"] <= 0:
            result = build_result(payload, "no long position, BUY_EXIT ignored")
            log_event(result)
            return {
                "ok": False,
                "reason": "no long position to exit",
                "result": result,
            }

        if price <= 0:
            result = build_result(payload, "invalid price, BUY_EXIT ignored")
            log_event(result)
            return {
                "ok": False,
                "reason": "invalid price",
                "result": result,
            }

        exit_qty = state["qty"]
        exit_avg = state["avg_price"]
        realized_pnl = round((price - exit_avg) * exit_qty, 4)

        state["realized_pnl"] = round(state["realized_pnl"] + realized_pnl, 4)

        if realized_pnl < 0:
            state["consecutive_losses"] += 1
        else:
            state["consecutive_losses"] = 0

        state["position_history"].append(
            {
                "action": "BUY_EXIT",
                "qty": exit_qty,
                "price": round(price, 4),
                "time": payload.time,
                "time_tw": utc_to_tw_str(payload.time),
                "avg_price": round(exit_avg, 4),
                "realized_pnl": realized_pnl,
            }
        )

        state["has_long"] = False
        state["qty"] = 0
        state["avg_price"] = 0.0
        state["last_action_key"] = action_key
        state["last_signal_time"] = payload.time
        state["last_signal_time_tw"] = utc_to_tw_str(payload.time)

        recalc_account_metrics(price)
        check_risk_lock()

        result = build_result(payload, f"SIM BUY_EXIT executed, pnl={realized_pnl}")
        log_event(result)
        return {
            "ok": True,
            "message": "BUY_EXIT received",
            "state": state,
            "result": result,
        }

    # -------------------------
    # 未知 action
    # -------------------------
    result = build_result(payload, f"unknown action: {action}")
    log_event(result)
    return {"ok": False, "reason": f"unknown action: {action}", "result": result}
