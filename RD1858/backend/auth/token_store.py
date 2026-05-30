"""
Token Store — File-based enctoken persistence
==============================================
Stores the enctoken in a local JSON file (config/enctoken.json).
Kite tokens expire at 06:00 IST the next morning, so the file is
sufficient for day-to-day operation.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from backend.core.config import Config
from backend.utils.logger import get_logger

logger = get_logger(__name__)


# ── Public API ────────────────────────────────────────────────────────────────

def save_token(user_id: str, enctoken: str) -> bool:
    """
    Persist enctoken to config/enctoken.json.
    Returns True on success, False on failure.
    """
    payload = json.dumps({
        "user_id":   user_id,
        "enctoken":  enctoken,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    try:
        Config.ENCTOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        Config.ENCTOKEN_FILE.write_text(payload)
        logger.info(f"✓ Enctoken saved to {Config.ENCTOKEN_FILE}")
        return True
    except Exception as exc:
        logger.error(f"Failed to save enctoken: {exc}")
        return False


def load_token() -> Optional[Dict]:
    """
    Load enctoken from config/enctoken.json.
    Returns a dict with keys user_id, enctoken, timestamp or None.
    """
    if not Config.ENCTOKEN_FILE.exists():
        return None

    try:
        data = json.loads(Config.ENCTOKEN_FILE.read_text())
        if data.get("user_id") and data.get("enctoken"):
            logger.debug("Loaded enctoken from local file")
            return data
        logger.warning("Enctoken file exists but is missing required fields")
        return None
    except Exception as exc:
        logger.warning(f"Enctoken file unreadable: {exc}")
        return None


def delete_token() -> None:
    """Remove enctoken file (logout)."""
    if Config.ENCTOKEN_FILE.exists():
        try:
            Config.ENCTOKEN_FILE.unlink()
            logger.info("Enctoken file deleted")
        except Exception as exc:
            logger.warning(f"Could not delete enctoken file: {exc}")
