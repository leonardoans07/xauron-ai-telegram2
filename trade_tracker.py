import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional

from storage import load_state, save_state
from config import STATE_FILE, MAX_ACTIVE_TRADES

@dataclass
class VirtualTrade:
    symbol: str
    tf: str               # ex: "5m"
    side: str             # "BUY" ou "SELL"
    score: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    created_at: float

    # flags
    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_tp3: bool = False
    hit_sl: bool = False
    closed: bool = False

    # último preço visto (evita aviso duplicado)
    last_price: Optional[float] = None

def _is_buy(side: str) -> bool:
    return side.strip().upper() == "BUY"

def _crossed(level: float, prev: Optional[float], now: float, up: bool) -> bool:
    """
    Detecta cruzamento. Se prev for None, usa 'encostou' simples.
    """
    if prev is None:
        return now >= level if up else now <= level
    return (prev < level <= now) if up else (prev > level >= now)

def add_trade(trade: VirtualTrade) -> None:
    state = load_state(STATE_FILE)
    trades: List[Dict[str, Any]] = state.get("trades", [])

    # limita quantidade
    active = [t for t in trades if not t.get("closed")]
    if len(active) >= MAX_ACTIVE_TRADES:
        # fecha o mais antigo automaticamente
        active_sorted = sorted(active, key=lambda x: x.get("created_at", 0))
        active_sorted[0]["closed"] = True

    trades.append(asdict(trade))
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
        if (t.get("created_at") == updated.created_at and
            t.get("symbol") == updated.symbol and
            t.get("side") == updated.side and
            t.get("entry") == updated.entry):
            trades[i] = asdict(updated)
            break
    state["trades"] = trades
    save_state(STATE_FILE, state)

def check_hits(tr: VirtualTrade, price: float) -> List[str]:
    """
    Retorna lista de eventos: ["TP1", "TP2", "TP3", "SL"] conforme bater.
    """
    events = []
    buy = _is_buy(tr.side)

    prev = tr.last_price
    tr.last_price = price

    # SL primeiro (se bater SL, encerra)
    if not tr.hit_sl and not tr.closed:
        if _crossed(tr.sl, prev, price, up=buy is False):  # BUY: SL abaixo (down); SELL: SL acima (up)
            # Para BUY, SL é abaixo: cruzar pra baixo => up=False
            # Para SELL, SL é acima: cruzar pra cima => up=True
            # A lógica acima não fica bonita, então fazemos explícito:
            pass

    # explícito (mais seguro)
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

    # TP1/TP2/TP3
    if not tr.hit_tp1 and not tr.closed:
        if buy and _crossed(tr.tp1, prev, price, up=True):
            tr.hit_tp1 = True
            events.append("TP1")
        if (not buy) and _crossed(tr.tp1, prev, price, up=False):
            tr.hit_tp1 = True
            events.append("TP1")

    if not tr.hit_tp2 and not tr.closed:
        if buy and _crossed(tr.tp2, prev, price, up=True):
            tr.hit_tp2 = True
            events.append("TP2")
        if (not buy) and _crossed(tr.tp2, prev, price, up=False):
            tr.hit_tp2 = True
            events.append("TP2")

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
