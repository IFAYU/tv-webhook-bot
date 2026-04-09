from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json
import os

app = FastAPI()

# =========================
# 基本設定
# =========================
SECRET = "abc123"
SIM_MODE = True
LOG_FILE = "trade_log.jsonl"

# 交易設定
OPEN_QTY = 2                   # 每次 BUY_OPEN 直接進 2 口微台
MAX_QTY = 4                    # 保留總上限設定（目前不加碼，先用不到）

# 風控設定
BLOCK_DUPLICATE_ALERT = True
DAILY_LOSS_LIMIT = -300.0      # 單日已實現虧損上限
HARD_STOP_PER_CONTRACT = 100.0 # 每口硬停損點數

# 模擬帳戶
INITIAL_CAPITAL = 1_000_000.0

# =========================
# 模擬持倉 / 帳戶狀態
# =========================
state = {
    "mode": "SIM" if SIM_MODE else "LIVE",
    "has_long": False,
    "qty": 0,
    "avg_price": 0.0,
    "last_action_key": None,
    "last_signal_time": None,
    "initial_capital": INITIAL_CAPITAL,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "equity": INITIAL_CAPITAL,
    "peak_equity": INITIAL_CAPITAL,
    "max_drawdown": 0.0,
    "last_price": 0.0,
    "position_history": [],
}

# =========================
# TradingView payload
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


def build_result(payload: TVPayload, note: str = "") -> dict:
    return {
        "received_at": datetime.now().isoformat(),
        "action": payload.action,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "strategy": payload.strategy,
        "price": payload.price,
        "time": payload.time,
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
        },
    }


# =========================
# 健康檢查
# =========================
@app.get("/health")
async def health():
    return {"ok": True, "state": state}


# =========================
# 最近紀錄
# =========================
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


# =========================
# 手動重置
# =========================
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
        "initial_capital": INITIAL_CAPITAL,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "equity": INITIAL_CAPITAL,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown": 0.0,
        "last_price": 0.0,
        "position_history": [],
    }

    return {"ok": True, "message": "state reset", "state": state}


# =========================
# Webhook 主入口
# =========================
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload):
    if payload.secret != SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")

    action_key = make_action_key(payload)
    action = payload.action
    price = safe_float(payload.price)

    # 每次收到訊號先更新最新價格 / 未實現損益 / equity
    recalc_account_metrics(price)

    # 1) 同K同動作只允許一次
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

    # 2) 日損上限
    if state["realized_pnl"] <= DAILY_LOSS_LIMIT:
        result = build_result(payload, "daily loss limit hit")
        log_event(result)
        return {
            "ok": False,
            "reason": "daily loss limit hit",
            "result": result,
        }

    # 3) 硬停損（持倉中即時計算）
    if state["has_long"] and state["unrealized_pnl"] <= -(HARD_STOP_PER_CONTRACT * state["qty"]):
        action = "BUY_EXIT"

    # 4) 防重複 alert
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

        recalc_account_metrics(price)

        state["position_history"].append(
            {
                "action": "BUY_OPEN",
                "qty": OPEN_QTY,
                "price": round(price, 4),
                "time": payload.time,
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

        state["position_history"].append(
            {
                "action": "BUY_EXIT",
                "qty": exit_qty,
                "price": round(price, 4),
                "time": payload.time,
                "avg_price": round(exit_avg, 4),
                "realized_pnl": realized_pnl,
            }
        )

        state["has_long"] = False
        state["qty"] = 0
        state["avg_price"] = 0.0
        state["last_action_key"] = action_key
        state["last_signal_time"] = payload.time

        recalc_account_metrics(price)

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
