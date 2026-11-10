"""HTTP surface. Run: uvicorn app.main:app --host 0.0.0.0 --port 8080

  POST /lark/events       Lark event subscription (im.message.receive_v1) — alerts and thread replies
  POST /pagerduty/status  Zoho Flow forwards every PagerDuty event here; the bot paints the one Lark card per incident
  GET  /health            liveness + last poll / sweep timestamps
"""
from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import config, db, lark_client, pipeline, workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alert-ir")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    workers.start()
    log.info("alert-ir-bot up. dry_run=%s poll=%ss alert_chat=%s", config.DRY_RUN, config.LARK_POLL_INTERVAL_SEC, config.ALERT_CHAT_ID)
    yield


app = FastAPI(title="alert-ir-bot", lifespan=lifespan)


@app.get("/health")
def health():
    now = time.time()
    body = {"ok": True, "dry_run": config.DRY_RUN, **workers.STATE}
    if config.LARK_POLL_INTERVAL_SEC > 0 and now - workers.STATE["last_poll_ok"] > 3 * config.LARK_POLL_INTERVAL_SEC + 60:
        body["ok"] = False
        body["error"] = "poller stale"
    if now - workers.STATE["last_sweep_ok"] > 3 * config.SWEEP_INTERVAL_SEC + 60:
        body["ok"] = False
        body["error"] = "sweeper stale"
    if not body["ok"]:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.post("/lark/events")
async def lark_events(request: Request, background: BackgroundTasks):
    body = await request.json()
    if "encrypt" in body:
        body = lark_client.decrypt_event(body["encrypt"])
    # one-time handshake when you save the callback URL in the Lark console
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    token = (body.get("header") or {}).get("token") or body.get("token") or ""
    if config.LARK_VERIFICATION_TOKEN and not hmac.compare_digest(token, config.LARK_VERIFICATION_TOKEN):
        raise HTTPException(status_code=401, detail="bad verification token")
    event_type = (body.get("header") or {}).get("event_type", "")
    if event_type == "card.action.trigger":
        # synchronous on purpose: the response body is the repainted card (Lark waits up to 3 s)
        return pipeline.handle_card_action(body.get("event") or {})
    if event_type != "im.message.receive_v1":
        log.info("lark event %s ignored", event_type or "(no type)")
        return {"ok": True, "ignored": event_type}
    msg = pipeline.normalize_event_message(body.get("event") or {})
    background.add_task(_handle_and_log, msg)  # Lark wants a fast 200; the work runs after
    return {"ok": True}


def _handle_and_log(msg: dict) -> None:
    """Lark pushes every message in every chat the bot is in; say what each one was and what we did."""
    try:
        result = pipeline.handle_message(msg)
    except Exception:  # noqa: BLE001
        log.exception("lark event: handling message %s failed", msg.get("message_id"))
        return
    log.info("lark event: %s in chat %s from %s -> %s", msg.get("message_type") or "?", msg.get("chat_id") or "?",
             msg.get("sender_id") or "?", result)


@app.post("/pagerduty/status")
async def pagerduty_status(request: Request, x_status_secret: str = Header(default="")):
    if config.PD_STATUS_SECRET and not hmac.compare_digest(x_status_secret, config.PD_STATUS_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")
    body = await request.json()
    return pipeline.handle_pd_status(body)