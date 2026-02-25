import json
import os
from typing import Any, Dict

def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"trades": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"trades": []}

def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
