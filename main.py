import logging
import os
import re
import threading
import time

from bot import build_application
from trade_tracker import list_active_trades, update_trade, check_hits
from price_feed import get_last_price
from config import TRACK_INTERVAL_SEC
from telegram_sender import send_telegram  # vamos criar este arquivo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")


# ================================
# TOKEN
# ================================

def read_token() -> str:
    return (os.getenv("TOKEN") or os.getenv("TELEGRAM") or "").strip()


def validate_token(token: str) -> None:
    if not token:
        raise RuntimeError("TOKEN vazio. Configure a variável TOKEN no Railway.")
    if token.lower() == "token":
        raise RuntimeError("TOKEN está como 'token' (placeholder). Cole o token real do @BotFather.")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise RuntimeError(f"TOKEN inválido (formato inesperado). Caracteres lidos: {len(token)}")


# ================================
# MONITOR DE TRADES (TP/SL)
# ================================

def format_hit_message(trade, hit: str, price: float) -> str:

    symbol = trade.symbol
    tf = trade.tf
    side = trade.side

    if hit == "SL":
        return (
            f"🛑 STOP LOSS atingido\n"
            f"{symbol} {tf} — {side}\n"
            f"Preço atual: {price}\n"
            f"SL: {trade.sl}\n"
            f"Trade encerrado."
        )

    if hit == "TP1":
        return (
            f"✅ TP1 atingido\n"
            f"{symbol} {tf} — {side}\n"
            f"Preço atual: {price}\n"
            f"TP1: {trade.tp1}"
        )

    if hit == "TP2":
        return (
            f"✅ TP2 atingido\n"
            f"{symbol} {tf} — {side}\n"
            f"Preço atual: {price}\n"
            f"TP2: {trade.tp2}"
        )

    if hit == "TP3":
        return (
            f"🏁 TP3 FINAL atingido\n"
            f"{symbol} {tf} — {side}\n"
            f"Preço atual: {price}\n"
            f"TP3: {trade.tp3}\n"
            f"Trade finalizado."
        )

    return ""


def trade_monitor_loop():

    log.info("Trade monitor iniciado.")

    while True:

        try:

            trades = list_active_trades()

            for trade in trades:

                price = get_last_price(trade.symbol)

                if price is None:
                    continue

                hits = check_hits(trade, price)

                if hits:

                    for hit in hits:

                        msg = format_hit_message(trade, hit, price)

                        if msg:
                            send_telegram(msg)

                    update_trade(trade)

        except Exception as e:
            log.error(f"Erro no trade monitor: {e}")

        time.sleep(max(5, TRACK_INTERVAL_SEC))


# ================================
# MAIN
# ================================

def main() -> None:

    token = read_token()
    validate_token(token)

    # inicia monitor em background
    thread = threading.Thread(
        target=trade_monitor_loop,
        daemon=True
    )
    thread.start()

    app = build_application(token)

    log.info("Bot iniciando (polling). Token lido com %s caracteres.", len(token))

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"]
    )


if __name__ == "__main__":
    main()
