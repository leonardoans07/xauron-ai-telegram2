import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Intervalo do monitoramento dos trades virtuais (segundos)
TRACK_INTERVAL_SEC = int(os.getenv("TRACK_INTERVAL_SEC", "15"))

# Quantos trades virtuais no máximo ficam abertos (evitar spam)
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", "20"))

# Arquivo de persistência local (Railway geralmente mantém no runtime; se reiniciar, perde)
STATE_FILE = os.getenv("STATE_FILE", "state_trades.json")
