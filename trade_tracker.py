import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from storage import load_state, save_state

STATE_FILE = "state_trades.json"


@dataclass
class VirtualTrade:
    chat_id: int
    symbol: str
    tf: str
    side: str          # "BUY" ou "SELL"
    score: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    created_at: float

    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_tp3: bool = False
    hit_sl: bool = False
    closed: bool = False

    last_price: Optional[float] = None


def add_trade(tr: VirtualTrade) -> None:
    state = load_state(STATE_FILE)
    trades: List[Dict[str, Any]] = state.get("trades", [])
    trades.append(asdict(tr))
    state["trades"] = trades
    save_state(STATE_FILE, state)


def list_active_trades() -> List[VirtualTrade]:
    state = load_state(STATE_FILE)
    out: List[VirtualTrade] = []
    for t in state.get("trades", []):
        if t.get("closed"):
            continue
        out.append(VirtualTrade(**t))
    return out


def update_trade(updated: VirtualTrade) -> None:
    state = load_state(STATE_FILE)
    trades = state.get("trades", [])
    for i, t in enumerate(trades):
        if (
            t.get("created_at") == updated.created_at
            and t.get("chat_id") == updated.chat_id
            and t.get("symbol") == updated.symbol
            and t.get("side") == updated.side
            and float(t.get("entry")) == float(updated.entry)
        ):
            trades[i] = asdict(updated)
            break
    state["trades"] = trades
    save_state(STATE_FILE, state)


def _crossed(level: float, prev: Optional[float], now: float, up: bool) -> bool:
    # Se não tem prev (primeira leitura), considera "tocou"
    if prev is None:
        return now >= level if up else now <= level
    return (prev < level <= now) if up else (prev > level >= now)


def check_hits(tr: VirtualTrade, price: float) -> List[str]:
    """
    Retorna eventos em ordem: ["SL"] ou ["TP1","TP2","TP3"] etc.
    Fecha no SL ou no TP3.
    """
    events: List[str] = []
    buy = tr.side.strip().upper() == "BUY"

    prev = tr.last_price
    tr.last_price = price

    # SL (encerra)
    if not tr.hit_sl and not tr.closed:
        if buy:
            if _crossed(tr.sl, prev, price, up=False):
                tr.hit_sl = True
                tr.closed = True
                events.append("SL")
                return events
        else:
            if _crossed(tr.sl, prev, price, up=True):
                tr.hit_sl = True
                tr.closed = True
                events.append("SL")
                return events

    # TP1
    if not tr.hit_tp1 and not tr.closed:
        if buy and _crossed(tr.tp1, prev, price, up=True):
            tr.hit_tp1 = True
            events.append("TP1")
        if (not buy) and _crossed(tr.tp1, prev, price, up=False):
            tr.hit_tp1 = True
            events.append("TP1")

    # TP2
    if not tr.hit_tp2 and not tr.closed:
        if buy and _crossed(tr.tp2, prev, price, up=True):
            tr.hit_tp2 = True
            events.append("TP2")
        if (not buy) and _crossed(tr.tp2, prev, price, up=False):
            tr.hit_tp2 = True
            events.append("TP2")

    # TP3 (encerra)
    if not tr.hit_tp3 and not tr.closed:
        if buy and _crossed(tr.tp3, prev, price, up=True):
            tr.hit_tp3 = True
            tr.closed = True
            events.append("TP3")
        if (not buy) and _crossed(tr.tp3, prev, price, up=False):
            tr.hit_tp3 = True
            tr.closed = True
            events.append("TP3")

    return events
