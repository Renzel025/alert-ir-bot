"""Background loops, started as daemon threads from main.py:
  * poll_loop  — reads the alert group (only if LARK_POLL_INTERVAL_SEC > 0; events are preferred)
  * sweep_loop — pages incidents whose check-person deadline passed, and retries failed pages
STATE feeds /health so an outside monitor can tell when this bot itself goes quiet."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict

from . import config, db, lark_client, pipeline

log = logging.getLogger("alert-ir")

STATE: Dict[str, float] = {"last_poll_ok": 0.0, "last_sweep_ok": 0.0, "started_at": time.time()}


def poll_loop() -> None:
    first = True
    while True:
        try:
            items = lark_client.list_messages(config.ALERT_CHAT_ID, 20)
            for item in reversed(items):  # oldest first
                msg = pipeline.normalize_list_item(item)
                if first:
                    db.mark_processed(msg["message_id"])  # backfill: never re-handle old alerts on first start
                    continue
                try:
                    pipeline.handle_message(msg)
                except Exception:  # noqa: BLE001 — one bad message must not abort the rest of the batch
                    log.exception("poll: handling %s failed", msg.get("message_id"))
            first = False
            STATE["last_poll_ok"] = time.time()
        except Exception:  # noqa: BLE001
            log.exception("poll failed")
        time.sleep(config.LARK_POLL_INTERVAL_SEC)


def sweep_loop() -> None:
    while True:
        try:
            n = pipeline.sweep_timeouts()
            if n:
                log.info("sweeper paged %s incident(s)", n)
            STATE["last_sweep_ok"] = time.time()
        except Exception:  # noqa: BLE001
            log.exception("sweep failed")
        time.sleep(config.SWEEP_INTERVAL_SEC)


def start() -> None:
    threading.Thread(target=sweep_loop, name="sweeper", daemon=True).start()
    if config.LARK_POLL_INTERVAL_SEC > 0:
        threading.Thread(target=poll_loop, name="poller", daemon=True).start()
