"""Drop-in for ~/Desktop/test-automation/main.py: tell alert-ir-bot the run result directly.

Add to .env of test-automation:
    ALERT_BOT_URL=https://alert-bot.mybot.ink/alerts
    ALERT_PUSH_SECRET=<same value as ALERT_PUSH_SECRET in alert-ir-bot/.env>

Then in run_automation(), right after the report card is posted (you have `facts`, `summary`, `duration`
and `posted` = the card's message id), call:
    notify_alert_bot(facts, summary, duration, posted)

A failed step opens an incident in the alert group, threaded under the run card; a fully green run for the
same provider resolves it. Never raises: a bot outage must not fail the test run.
"""
import os

import requests


def notify_alert_bot(facts, summary, duration_seconds, card_message_id=None):
    url = os.getenv("ALERT_BOT_URL", "").strip()
    secret = os.getenv("ALERT_PUSH_SECRET", "").strip()
    if not url or not secret:
        return
    record = facts.get("bet_record") or {}
    steps = [
        {"step": "Enter Casino Plus", "ok": bool(facts.get("login_success"))},
        {"step": "Enter provider game", "ok": bool(facts.get("game_accessible"))},
        {"step": "Place bet",
         "ok": True if facts.get("bet_verified") else ("warn" if facts.get("bet_placed") else False),
         "note": "" if facts.get("bet_verified") else (facts.get("bet_note") or "")},
    ]
    if record and not record.get("pending"):
        steps.append({"step": "Bet history", "ok": record.get("ok") is True, "note": record.get("note") or ""})
    provider = facts.get("opened_provider") or facts.get("game") or "unknown"
    if facts.get("provider_id"):
        provider = f"{provider} {facts['provider_id']}"
    body = {
        "source": "test-automation",
        "provider": provider,
        "game": facts.get("game") or "",
        "steps": steps,
        "summary": summary or "",
        "duration": f"{duration_seconds:.0f}s",
        "lark_message_id": card_message_id or "",
        "errors": (facts.get("errors") or [])[:5],
    }
    try:
        r = requests.post(url, json=body, headers={"X-Alert-Secret": secret}, timeout=10)
        print(f"alert-ir-bot: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"alert-ir-bot notify failed: {type(e).__name__}: {e}")
