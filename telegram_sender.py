import os
import requests

TOKEN = (os.getenv("TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()


def send_telegram(text: str):

    if not TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=10
        )
    except:
        pass
