import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from trade_tracker import VirtualTrade, add_trade, list_active_trades, update_trade, check_hits

log = logging.getLogger("bot")

# =============================================================================
# ENV
# =============================================================================
TWELVE_API_KEY = (os.getenv("TWELVE_API_KEY") or "").strip()

DEFAULT_INTERVAL = (os.getenv("DEFAULT_INTERVAL") or "5min").strip()
DEFAULT_SYMBOLS = (os.getenv("DEFAULT_SYMBOLS") or "XAUUSD").strip()          # ex: "XAUUSD,EURUSD,BTCUSD"
AUTO_TFS = (os.getenv("AUTO_TFS") or "1min,5min,15min,1h").strip()            # timeframes do scan
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS") or "60")       # a cada 60s

# Monitor TP/SL virtual
TRACK_INTERVAL_SECONDS = int(os.getenv("TRACK_INTERVAL_SECONDS") or "15")     # a cada 15s

VI_LENGTH = int(os.getenv("VI_LENGTH") or "14")
ATR_LENGTH = int(os.getenv("ATR_LENGTH") or "14")

ATR_SL_MULT = float(os.getenv("ATR_SL_MULT") or "1.5")
ATR_TP1_MULT = float(os.getenv("ATR_TP1_MULT") or "1.0")
ATR_TP2_MULT = float(os.getenv("ATR_TP2_MULT") or "2.0")
ATR_TP3_MULT = float(os.getenv("ATR_TP3_MULT") or "3.0")

MIN_STRENGTH = float(os.getenv("MIN_STRENGTH") or "0.12")   # força mínima VI para considerar
EMA_LENGTH = int(os.getenv("EMA_LENGTH") or "200")           # filtro de tendência
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT") or "0.001")     # 0.1% do preço (anti-lateral)
MIN_SCORE = int(os.getenv("MIN_SCORE") or "70")              # score mínimo para alertar

MTF_FILTER = (os.getenv("MTF_FILTER") or "1").strip() == "1"
MTF_TIMEFRAME = (os.getenv("MTF_TIMEFRAME") or "1h").strip() # timeframe maior (filtro)

# Anti-spam: último estado por (chat, symbol, tf)
LAST_STATE: Dict[Tuple[int, str, str], str] = {}

# Config por chat
AUTO_ENABLED: Dict[int, bool] = {}
AUTO_TFS_BY_CHAT: Dict[int, List[str]] = {}
AUTO_SYMBOLS_BY_CHAT: Dict[int, List[str]] = {}


@dataclass
class Candle:
    t: str
    o: float
    h: float
    l: float
    c: float


# =============================================================================
# SYMBOL PARSING
# =============================================================================
def _normalize_symbol(raw: str) -> str:
    s = raw.strip().upper().replace("#", "").replace("$", "")
    if "/" in s:
        return s
    # XAUUSD -> XAU/USD, BTCUSD -> BTC/USD, EURUSD -> EUR/USD
    if re.match(r"^[A-Z]{6}$", s):
        return f"{s[:3]}/{s[3:]}"
    return s


def _extract_symbol_and_interval(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None

    parts = text.strip().split()
    if not parts:
        return None, None

    first = parts[0].strip()
    if first.startswith("/"):
        return None, None

    sym = _normalize_symbol(first)
    if not re.match(r"^[A-Z0-9._-]{3,15}(\/[A-Z0-9._-]{3,15})?$", sym):
        return None, None

    interval = None
    if len(parts) >= 2:
        raw = parts[1].strip().upper()
        m_map = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}
        interval = m_map.get(raw, parts[1].strip().lower())

    return sym, interval


def _parse_csv_list(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


# =============================================================================
# TWELVE DATA
# =============================================================================
async def fetch_candles_twelve(symbol: str, interval: str, outputsize: int = 260) -> List[Candle]:
    if not TWELVE_API_KEY:
        raise RuntimeError("TWELVE_API_KEY não configurada no Railway.")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }

    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message', 'unknown error')}")

    values = data.get("values") if isinstance(data, dict) else None
    if not values:
        raise RuntimeError("TwelveData não retornou candles (values vazio).")

    candles: List[Candle] = []
    for row in reversed(values):  # inverter (mais antigo -> mais recente)
        candles.append(Candle(
            t=row["datetime"],
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
        ))
    return candles


async def fetch_price_twelve(symbol: str) -> Optional[float]:
    """
    Pega preço atual (bem mais barato que candles).
    Endpoint: /price
    """
    if not TWELVE_API_KEY:
        return None

    url = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": TWELVE_API_KEY, "format": "JSON"}

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and data.get("status") == "error":
        # Sem crédito / erro da API
        return None

    p = data.get("price") if isinstance(data, dict) else None
    if not p:
        return None
    try:
        return float(p)
    except Exception:
        return None


# =============================================================================
# INDICATORS
# =============================================================================
def _true_range(curr: Candle, prev_close: float) -> float:
    return max(curr.h - curr.l, abs(curr.h - prev_close), abs(curr.l - prev_close))


def atr(candles: List[Candle], length: int) -> float:
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para ATR.")
    trs = []
    for i in range(1, len(candles)):
        trs.append(_true_range(candles[i], candles[i - 1].c))
    window = trs[-length:]
    return sum(window) / len(window)


def vortex(candles: List[Candle], length: int) -> Tuple[float, float]:
    # Mantém a lógica igual; só o nome exibido é XAURON.
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para cálculo.")
    vm_plus, vm_minus, tr = [], [], []

    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        vm_plus.append(abs(c.h - p.l))
        vm_minus.append(abs(c.l - p.h))
        tr.append(_true_range(c, p.c))

    vm_plus_w = vm_plus[-length:]
    vm_minus_w = vm_minus[-length:]
    tr_w = tr[-length:]

    sum_tr = sum(tr_w) if sum(tr_w) != 0 else 1e-9
    vi_plus = sum(vm_plus_w) / sum_tr
    vi_minus = sum(vm_minus_w) / sum_tr
    return vi_plus, vi_minus


def ema(values: List[float], length: int) -> float:
    if len(values) < length:
        raise RuntimeError("Poucos valores para EMA.")
    k = 2 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr_percent(atr_val: float, price: float) -> float:
    if price == 0:
        return 0.0
    return atr_val / price


# =============================================================================
# SIGNAL + PLAN
# =============================================================================
def decide_signal(vi_p: float, vi_m: float) -> Tuple[str, float]:
    strength = abs(vi_p - vi_m)
    if strength < MIN_STRENGTH:
        return "WAIT", strength
    return ("BUY" if vi_p > vi_m else "SELL"), strength


def build_trade_plan(last_price: float, direction: str, atr_val: float) -> Dict[str, float]:
    entry = last_price
    if direction == "BUY":
        sl = entry - atr_val * ATR_SL_MULT
        tp1 = entry + atr_val * ATR_TP1_MULT
        tp2 = entry + atr_val * ATR_TP2_MULT
        tp3 = entry + atr_val * ATR_TP3_MULT
    else:
        sl = entry + atr_val * ATR_SL_MULT
        tp1 = entry - atr_val * ATR_TP1_MULT
        tp2 = entry - atr_val * ATR_TP2_MULT
        tp3 = entry - atr_val * ATR_TP3_MULT
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}


def fmt_price(x: float) -> str:
    return f"{x:.2f}" if x >= 100 else f"{x:.5f}"


def format_alert(symbol: str, interval: str, signal: str, strength: float, vi_p: float, vi_m: float, atr_val: float, plan: Dict[str, float], score: int) -> str:
    return (
        f"🚨 *ALERTA XAURON*\n"
        f"• Ativo: *{symbol}*\n"
        f"• TF: *{interval}*\n\n"
        f"✅ *Sinal:* *{signal}*\n"
        f"🎯 Entrada: `{fmt_price(plan['entry'])}`\n"
        f"🛡 Stop: `{fmt_price(plan['sl'])}`\n"
        f"🏁 TP1: `{fmt_price(plan['tp1'])}` | TP2: `{fmt_price(plan['tp2'])}` | TP3: `{fmt_price(plan['tp3'])}`\n\n"
        f"🔎 VI+ `{vi_p:.3f}` vs VI- `{vi_m:.3f}` | Força `{strength:.3f}` | ATR `{atr_val:.3f}`\n"
        f"⭐ Score: *{score}/100*\n\n"
        f"⏳ Monitorando TP/SL..."
    )


async def analyze_once(symbol: str, interval: str) -> Tuple[str, Dict[str, float], float, float, float, float, int]:
    candles = await fetch_candles_twelve(symbol, interval, outputsize=260)
    closes = [c.c for c in candles]
    last = candles[-1]
    last_price = last.c

    vi_p, vi_m = vortex(candles, VI_LENGTH)
    atr_val = atr(candles, ATR_LENGTH)

    signal, strength = decide_signal(vi_p, vi_m)
    direction = "BUY" if vi_p > vi_m else "SELL"
    plan = build_trade_plan(last_price, direction, atr_val)

    ema_val = ema(closes[-EMA_LENGTH:], EMA_LENGTH)
    trend_ok = (last_price > ema_val) if direction == "BUY" else (last_price < ema_val)

    apct = atr_percent(atr_val, last_price)
    vol_ok = apct >= MIN_ATR_PCT

    mtf_ok = True
    if MTF_FILTER:
        candles_htf = await fetch_candles_twelve(symbol, MTF_TIMEFRAME, outputsize=220)
        vi_p_htf, vi_m_htf = vortex(candles_htf, VI_LENGTH)
        htf_dir = "BUY" if vi_p_htf > vi_m_htf else "SELL"
        mtf_ok = (htf_dir == direction)

    score = 0
    score += 30 if mtf_ok else 0
    score += 25 if trend_ok else 0
    score += 20 if strength >= (MIN_STRENGTH + 0.10) else (10 if strength >= MIN_STRENGTH else 0)
    score += 15 if vol_ok else 0
    score += 10 if ((direction == "BUY" and last.c >= last.o) or (direction == "SELL" and last.c <= last.o)) else 0

    if signal == "WAIT" or (MTF_FILTER and not mtf_ok) or (not trend_ok) or (not vol_ok) or (score < MIN_SCORE):
        return "WAIT", plan, strength, vi_p, vi_m, atr_val, score

    return signal, plan, strength, vi_p, vi_m, atr_val, score


# =============================================================================
# TRADE MONITOR JOB (TP/SL VIRTUAL)
# =============================================================================
def _hit_msg(tr: VirtualTrade, hit: str, price: float) -> str:
    if hit == "SL":
        return (
            f"🛑 *STOP LOSS atingido*\n"
            f"• Ativo: *{tr.symbol}*\n"
            f"• TF: *{tr.tf}*\n"
            f"• Direção: *{tr.side}*\n"
            f"• Preço: `{fmt_price(price)}`\n"
            f"• SL: `{fmt_price(tr.sl)}`\n"
            f"✅ Trade encerrado."
        )
    if hit == "TP1":
        return (
            f"✅ *TP1 atingido*\n"
            f"{tr.symbol} {tr.tf} — *{tr.side}*\n"
            f"Preço: `{fmt_price(price)}` | TP1: `{fmt_price(tr.tp1)}`"
        )
    if hit == "TP2":
        return (
            f"✅ *TP2 atingido*\n"
            f"{tr.symbol} {tr.tf} — *{tr.side}*\n"
            f"Preço: `{fmt_price(price)}` | TP2: `{fmt_price(tr.tp2)}`"
        )
    if hit == "TP3":
        return (
            f"🏁 *TP3 FINAL atingido*\n"
            f"• Ativo: *{tr.symbol}*\n"
            f"• TF: *{tr.tf}*\n"
            f"• Direção: *{tr.side}*\n"
            f"• Preço: `{fmt_price(price)}`\n"
            f"• TP3: `{fmt_price(tr.tp3)}`\n"
            f"✅ Trade finalizado."
        )
    return ""


async def trade_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    trades = list_active_trades()

    for tr in trades:
        try:
            price = await fetch_price_twelve(tr.symbol)
            if price is None:
                continue

            hits = check_hits(tr, float(price))
            if hits:
                for h in hits:
                    msg = _hit_msg(tr, h, float(price))
                    if msg:
                        await app.bot.send_message(chat_id=tr.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                update_trade(tr)

        except Exception as e:
            log.warning("Monitor erro %s %s: %s", tr.symbol, tr.tf, e)


# =============================================================================
# COMMANDS
# =============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    AUTO_ENABLED.setdefault(chat_id, False)
    AUTO_TFS_BY_CHAT.setdefault(chat_id, _parse_csv_list(AUTO_TFS))
    AUTO_SYMBOLS_BY_CHAT.setdefault(chat_id, _parse_csv_list(DEFAULT_SYMBOLS))

    await update.message.reply_text(
        "👋 *XAURON Scanner*\n\n"
        "📌 Consulta manual:\n"
        "• `XAUUSD`\n"
        "• `XAUUSD 5min`\n\n"
        "🤖 Scanner automático:\n"
        "• `/autoscan on`\n"
        "• `/autoscan off`\n"
        "• `/settf 1min,5min,15min,1h`\n"
        "• `/setsymbols XAUUSD,EURUSD,BTCUSD`\n",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Comandos:\n"
        "• `XAUUSD` (ou `XAU/USD`) → análise na hora\n"
        "• `/autoscan on` → começa o scanner\n"
        "• `/autoscan off` → para\n"
        "• `/settf 1min,5min,15min,1h`\n"
        "• `/setsymbols XAUUSD,EURUSD,BTCUSD`\n\n"
        "📌 Dica: quanto maior o *MIN_SCORE*, mais seletivo e assertivo.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text("Use: `/autoscan on` ou `/autoscan off`", parse_mode=ParseMode.MARKDOWN)
        return
    AUTO_ENABLED[chat_id] = (arg == "on")
    await update.message.reply_text(f"Scanner automático: *{arg.upper()}*", parse_mode=ParseMode.MARKDOWN)


async def settf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Use: `/settf 1min,5min,15min,1h`", parse_mode=ParseMode.MARKDOWN)
        return
    tfs = _parse_csv_list(raw)
    AUTO_TFS_BY_CHAT[chat_id] = tfs
    await update.message.reply_text(f"Timeframes do scanner: `{', '.join(tfs)}`", parse_mode=ParseMode.MARKDOWN)


async def setsymbols(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Use: `/setsymbols XAUUSD,EURUSD,BTCUSD`", parse_mode=ParseMode.MARKDOWN)
        return
    syms = _parse_csv_list(raw)
    AUTO_SYMBOLS_BY_CHAT[chat_id] = syms
    await update.message.reply_text(f"Ativos do scanner: `{', '.join(syms)}`", parse_mode=ParseMode.MARKDOWN)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    symbol, interval = _extract_symbol_and_interval(text)

    if not symbol:
        await update.message.reply_text("Manda só o ativo (ex: `XAUUSD`).", parse_mode=ParseMode.MARKDOWN)
        return

    interval = interval or DEFAULT_INTERVAL
    chat_id = update.effective_chat.id

    try:
        await update.message.reply_text("⏳ Pegando candles + calculando XAURON…", parse_mode=ParseMode.MARKDOWN)

        signal, plan, strength, vi_p, vi_m, atr_val, score = await analyze_once(symbol, interval)
        if signal == "WAIT":
            await update.message.reply_text(
                f"⏳ *WAIT* — sem setup forte agora em *{symbol}* no *{interval}* (score `{score}/100`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        msg = format_alert(symbol, interval, signal, strength, vi_p, vi_m, atr_val, plan, score)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

        # REGISTRA TRADE VIRTUAL (para avisar TP/SL depois)
        add_trade(VirtualTrade(
            chat_id=chat_id,
            symbol=symbol,
            tf=interval,
            side=signal,
            score=score,
            entry=float(plan["entry"]),
            sl=float(plan["sl"]),
            tp1=float(plan["tp1"]),
            tp2=float(plan["tp2"]),
            tp3=float(plan["tp3"]),
            created_at=time.time(),
        ))

    except Exception as e:
        log.exception("Erro: %s", e)
        await update.message.reply_text(f"Erro: `{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# =============================================================================
# BACKGROUND SCANNER JOB
# =============================================================================
async def autoscan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application

    for chat_id, enabled in list(AUTO_ENABLED.items()):
        if not enabled:
            continue

        tfs = AUTO_TFS_BY_CHAT.get(chat_id) or _parse_csv_list(AUTO_TFS)
        syms_raw = AUTO_SYMBOLS_BY_CHAT.get(chat_id) or _parse_csv_list(DEFAULT_SYMBOLS)

        for raw_sym in syms_raw:
            symbol = _normalize_symbol(raw_sym)

            for tf in tfs:
                try:
                    signal, plan, strength, vi_p, vi_m, atr_val, score = await analyze_once(symbol, tf)

                    key = (chat_id, symbol, tf)
                    prev = LAST_STATE.get(key, "NONE")

                    # manda só quando muda para BUY/SELL
                    if signal in ("BUY", "SELL") and signal != prev:
                        LAST_STATE[key] = signal
                        msg = format_alert(symbol, tf, signal, strength, vi_p, vi_m, atr_val, plan, score)
                        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                        # REGISTRA TRADE VIRTUAL
                        add_trade(VirtualTrade(
                            chat_id=chat_id,
                            symbol=symbol,
                            tf=tf,
                            side=signal,
                            score=score,
                            entry=float(plan["entry"]),
                            sl=float(plan["sl"]),
                            tp1=float(plan["tp1"]),
                            tp2=float(plan["tp2"]),
                            tp3=float(plan["tp3"]),
                            created_at=time.time(),
                        ))

                    # atualiza WAIT sem mandar msg (anti-spam)
                    if signal == "WAIT":
                        LAST_STATE[key] = "WAIT"

                except Exception as e:
                    log.warning("Scanner erro %s %s: %s", raw_sym, tf, e)


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("autoscan", autoscan))
    app.add_handler(CommandHandler("settf", settf))
    app.add_handler(CommandHandler("setsymbols", setsymbols))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Scanner a cada X segundos
    app.job_queue.run_repeating(autoscan_job, interval=SCAN_INTERVAL_SECONDS, first=10)

    # Monitor TP/SL virtual (separado)
    app.job_queue.run_repeating(trade_monitor_job, interval=TRACK_INTERVAL_SECONDS, first=15)

    return app
