"""
Position Slot Tracker
======================
Tracks how many buy tranches make up a symbol's CURRENT open position, for
display in the dashboard's Slot Matrix widget.

This is intentionally separate from SignalGenerator._buys_today, which is a
daily trading-permission counter that resets every midnight (see the
"SLOT RULES" docstring at the top of signal_generator.py — that counter
governs how many *new* buys the bot is allowed to place *today*, and is
correct to reset nightly).

The Slot Matrix, by contrast, needs to answer a different question: "how many
of my max_slots tranches are already deployed into this open position?" That
answer must NOT reset at midnight — a position built up over several days
should keep showing its true slot usage until the position is fully closed.

Persistence:
  Counts are stored in config/position_slots.json ({symbol: count}) so they
  survive bot restarts. Writes are atomic (temp file + os.replace) to avoid
  readers ever seeing a partially-written file.

Lifecycle:
  - increment(symbol): call once per successful BUY execution (same trigger
    point as SignalGenerator.record_buy_executed).
  - reset(symbol):      call once a symbol's position is fully closed (the
    bot's sell logic always sells the entire held quantity in one order, so
    a successful sell fill always means a full exit — safe to zero out here).
  - ensure_seeded(symbol): called for symbols currently held that have no
    persisted count yet (e.g. positions that existed before this tracker was
    introduced, or built up before a bot restart lost in-memory-only state).
    Seeds to 1 rather than 0 — a symbol that's genuinely held has consumed
    at least one slot, and 0 would misrepresent a live position as untouched.
"""
import json
import os
import threading
from pathlib import Path
from typing import Dict

from backend.utils.logger import get_logger

logger = get_logger(__name__)

_STATE_PATH = Path(__file__).parent.parent.parent / 'config' / 'position_slots.json'


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


class PositionSlotTracker:
    """Persistent, per-symbol count of buy tranches in the current open position."""

    def __init__(self, path: Path = _STATE_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = self._load()

    def _load(self) -> Dict[str, int]:
        try:
            if self._path.exists():
                with open(self._path) as f:
                    data = json.load(f)
                    return {k: int(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[position_slots] Could not read {self._path}: {e}")
        return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._path, self._counts)
        except Exception as e:
            logger.warning(f"[position_slots] Could not write {self._path}: {e}")

    def get(self, symbol: str) -> int:
        with self._lock:
            return self._counts.get(symbol, 0)

    def increment(self, symbol: str) -> int:
        with self._lock:
            new_count = self._counts.get(symbol, 0) + 1
            self._counts[symbol] = new_count
            self._save()
            logger.info(f"📌 {symbol}: position slot {new_count} recorded (persists across days)")
            return new_count

    def reset(self, symbol: str) -> None:
        """Zero out a symbol's slot count — call after a full exit (sell)."""
        with self._lock:
            if symbol in self._counts and self._counts[symbol] != 0:
                self._counts[symbol] = 0
                self._save()
                logger.info(f"📌 {symbol}: position slots reset to 0 (position closed)")

    def ensure_seeded(self, symbol: str) -> None:
        """Back-fill a count of 1 for a currently-held symbol with no prior record.

        We have no way to reconstruct the true historical tranche count for
        positions that predate this tracker (Zerodha's live order-book API only
        exposes the current trading day), so 1 is the safest floor: it reflects
        "at least one slot is in use" rather than the misleading "0 used".
        """
        with self._lock:
            if symbol not in self._counts:
                self._counts[symbol] = 1
                self._save()
                logger.info(f"📌 {symbol}: position slots seeded to 1 (pre-existing holding, no prior record)")
