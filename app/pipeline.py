"""The flow from the diagram, as code.

    MonitorFlow alert (posted in the Lark alert group)
      -> bot receives: dedupe, log
      -> policy check: always-page class?        yes -> PagerDuty
      -> AI triage: escalate -> PagerDuty | can wait -> thread + tag check person
      -> check person replies within N min: normal -> resolve | escalate / silence -> PagerDuty
    PagerDuty -> on-call called -> Zoho Flow posts status in the thread and POSTs it to us.

Rules that never change:
  * Anything that fails here (AI down, Lark error, no policy) pages. Never drop.
  * Only the tagged check person can say "normal". Anyone can say "escalate".
  * Silence is not "normal": the sweeper pages when the deadline passes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from . import ai_notes, ai_triage, alert_parser, cards, config, db, lark_client, pagerduty
from . import policy as policy_mod

log = logging.getLogger("alert-ir")

# Words that decide a check person's reply. Escalate words are checked first so
# "not normal" never counts as "normal".
_ESCALATE_WORDS = [
    "escalate", "page", "call", "not normal", "abnormal", "not ok", "not okay",
    "real issue", "problem", "broken", "down", "urgent", "p0",
]
_NORMAL_WORDS = [
    "its normal", "is normal", "its okay", "its fine", "false alarm", "expected", "no issue",
    "ignore", "all good", "is safe", "resolved",
]


# --- message normalisation: event payloads and list-messages items look different ------

def normalize_event_message(event: Dict[str, Any]) -> Dict[str, Any]:
    msg = event.get("message") or {}
    sender = event.get("sender") or {}
    sid = sender.get("sender_id") or {}
    return {
        "message_id": msg.get("message_id", ""),
        "chat_id": msg.get("chat_id", ""),
        "sender_id": sid.get("open_id") or sid.get("user_id") or sid.get("union_id") or sender.get("id", ""),
        "sender_type": sender.get("sender_type", ""),
        "message_type": msg.get("message_type", ""),
        "content": msg.get("content", ""),
        "root_id": msg.get("root_id", ""),
        "parent_id": msg.get("parent_id", ""),
        "thread_id": msg.get("thread_id", ""),
    }


def normalize_list_item(item: Dict[str, Any]) -> Dict[str, Any]:
    sender = item.get("sender") or {}
    return {
        "message_id": item.get("message_id", ""),
        "chat_id": item.get("chat_id", ""),
        "sender_id": sender.get("id", ""),
        "sender_type": sender.get("sender_type", ""),
        "message_type": item.get("msg_type", ""),
        "content": (item.get("body") or {}).get("content", ""),
        "root_id": item.get("root_id", ""),
        "parent_id": item.get("parent_id", ""),
        "thread_id": item.get("thread_id", ""),
    }


# --- entry point ---------------------------------------------------------------------

def handle_message(msg: Dict[str, Any]) -> str:
    """Used by both event and polling mode. Returns a short label of what happened."""
    mid = msg.get("message_id") or ""
    if not mid or not db.mark_processed(mid):
        return "duplicate"
    if msg.get("chat_id") not in (config.ALERT_CHAT_ID, config.FIXED_GROUP_CHAT_ID):
        return "other_chat"
    text = alert_parser.extract_text(msg.get("message_type", ""), msg.get("content", ""))

    # 1) an alert from the MonitorFlow bot
    if msg.get("chat_id") == config.ALERT_CHAT_ID and msg.get("sender_id") == config.ALERT_BOT_SENDER_ID:
        return _handle_alert(msg, text)

    # 2) a human reply inside an incident thread
    if msg.get("sender_type") and msg["sender_type"] != "user":
        return "ignored_bot"
    incident = db.find_incident_by_thread([msg.get("root_id", ""), msg.get("thread_id", ""), msg.get("parent_id", "")])
    if incident:
        return _handle_thread_reply(msg, incident, text)
    return "ignored"


# --- the alert path ------------------------------------------------------------------

def _handle_alert(msg: Dict[str, Any], text: str) -> str:
    # MonitorFlow posts cards; the text template is kept for simulate.sh
    alert = alert_parser.parse_card(msg.get("content", "")) if msg.get("message_type") == "interactive" else None
    if alert is None:
        alert = alert_parser.parse(text)
    if alert is None:
        log.info("alert-bot message did not match template: %r", text[:120])
        return "unparsed"
    if alert.is_test:
        lark_client.reply_text(msg["message_id"], "✅ test received", in_thread=True)
        return "test"

    existing = db.find_open_incident(alert.fingerprint)

    if alert.status == "resolved":
        if existing:
            _resolve_from_monitor(existing["id"])
            return "resolved"
        return "resolved_unknown"

    if existing:
        db.update_incident(existing["id"], alert_json=json.dumps(alert.to_dict(), ensure_ascii=False))
        _reply(existing["id"], f"🔁 Still firing: {alert.firing_count} instance(s), alert time {alert.alert_time or '?'}.")
        return "repeat"

    pol = policy_mod.lookup(alert.rule_name, alert.severity)
    incident = db.create_incident(
        fingerprint=alert.fingerprint,
        rule_name=alert.rule_name,
        severity=alert.severity,
        chat_id=config.FIXED_GROUP_CHAT_ID,
        alert_message_id=msg["message_id"],
        pd_dedup_key=f"{alert.fingerprint}-{int(time.time())}",
        policy_json=json.dumps(pol.to_dict(), ensure_ascii=False),
        alert_json=json.dumps(alert.to_dict(), ensure_ascii=False),
    )
    iid = int(incident["id"])
    try:
        _open_thread(iid, msg["message_id"], alert)
    except Exception as e:  # noqa: BLE001 — a Lark failure must never stop the alert from paging
        log.exception("could not open the thread for incident %s", iid)
        db.log_decision(iid, "lark", "error", f"{type(e).__name__}: {e}"[:500])
        if not db.get_incident(iid).get("thread_root_message_id"):
            db.update_incident(iid, thread_root_message_id=msg["message_id"])  # fall back to the alert message

    # 3) policy check
    db.log_decision(iid, "policy", "page" if pol.always_page else "triage", f"matched={pol.matched}", pol.to_dict())
    if pol.always_page:
        page(iid, reason=f"Always-page alert class ({pol.matched}).", outcome="always_page")
        return "paged_policy"

    # 4) AI triage — any failure pages
    try:
        result = ai_triage.triage(alert, pol, db.past_cases(alert.rule_name))
    except Exception as e:  # noqa: BLE001 — fail-safe by design
        log.exception("AI triage failed for incident %s", iid)
        db.log_decision(iid, "ai", "error", f"{type(e).__name__}: {e}"[:500])
        page(iid, reason=f"AI triage unavailable ({type(e).__name__}); paging to be safe.", outcome="ai_error")
        return "paged_ai_error"
    db.log_decision(iid, "ai", result.decision, result.reasoning, {"confidence": result.confidence})
    if result.decision != "can_wait":
        page(iid, reason=f"AI: {result.reasoning}", outcome="ai_escalate")
        return "paged_ai"

    # 5) can wait → tag the check person and start the clock
    if not pol.check_person:
        page(iid, reason="No check person configured for this alert; paging to be safe.", outcome="no_check_person")
        return "paged_no_check_person"
    deadline = time.time() + pol.wait_minutes * 60
    db.update_incident(iid, status="waiting", check_person_open_id=pol.check_person, wait_deadline=deadline)
    _reply(
        iid,
        f"🟡 AI says this can wait (confidence {result.confidence}): {result.reasoning}\n"
        f"{lark_client.mention(pol.check_person, pol.check_person_name)} please check and reply in this thread within "
        f"{pol.wait_minutes} min: 'normal' or 'escalate'. No reply = I page on-call at "
        f"{time.strftime('%H:%M', time.localtime(deadline))}.",
        in_thread=False,     # verdicts go in the group chat, where everyone sees them; replies come back in the thread
    )
    return "waiting"


def _open_thread(incident_id: int, alert_message_id: str, alert: alert_parser.Alert) -> None:
    """Same group as the alerts: the alert message is the thread root. Different
    fixed group: post our own root message there."""
    header = f"[{alert.severity or '?'}] {alert.rule_name} · {alert.firing_count} instance(s) · {alert.alert_time or ''}"
    if config.FIXED_GROUP_CHAT_ID == config.ALERT_CHAT_ID:
        data = lark_client.reply_text(alert_message_id, f"👀 Incident #{incident_id} opened. {header}\nTriaging…", in_thread=True)
        db.update_incident(incident_id, thread_root_message_id=alert_message_id, thread_id=data.get("thread_id", ""))
    else:
        data = lark_client.post_text(config.FIXED_GROUP_CHAT_ID, f"👀 Incident #{incident_id}: {header}\n{alert.alert_value}")
        db.update_incident(incident_id, thread_root_message_id=data.get("message_id", ""), thread_id=data.get("thread_id", ""))


# --- the check-person path -------------------------------------------------------------

def classify_reply(text: str) -> Optional[str]:
    """'normal' | 'escalate' | None (unclear). Keywords first; the AI classifier is optional."""
    low = (text or "").lower()
    for w in _ESCALATE_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return "escalate"
    for w in _NORMAL_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return "normal"
    try:
        return ai_triage.classify_reply(text)
    except Exception:  # noqa: BLE001 — classifier not wired yet or AI down: stay unclear
        return None


def _handle_thread_reply(msg: Dict[str, Any], incident: Dict[str, Any], text: str) -> str:
    iid = int(incident["id"])
    if incident["status"] != "waiting":
        db.log_decision(iid, "reply", "context", text[:500], {"sender": msg.get("sender_id", "")})
        return "reply_context"
    verdict = classify_reply(text)
    is_check_person = msg.get("sender_id") == incident.get("check_person_open_id")
    db.log_decision(iid, "reply", verdict or "unclear", text[:500],
                    {"sender": msg.get("sender_id", ""), "is_check_person": is_check_person})

    if verdict == "escalate":
        who = "Check person" if is_check_person else "A teammate"
        page(iid, reason=f"{who} replied: \"{text[:100]}\"", outcome="human_escalate", actor=msg.get("sender_id", ""))
        return "paged_reply"
    if verdict == "normal":
        if not is_check_person:
            _reply(iid, "Noted, but only the tagged check person can mark this normal. Timer still running.")
            return "reply_not_authorized"
        resolve_normal(iid, actor=msg.get("sender_id", ""), text=text)
        return "resolved_normal"
    if is_check_person:
        _reply(iid, "I couldn't tell if that means normal or escalate. Please reply 'normal' or 'escalate'. Timer still running.")
    return "reply_unclear"


def resolve_normal(incident_id: int, actor: str, text: str) -> None:
    db.update_incident(incident_id, status="resolved", wait_deadline=None, resolved_at=time.time())
    db.log_outcome(incident_id, "human_normal", actor=actor, detail=text[:500])
    # in the group chat, not the thread: everyone sees the verdict without opening anything
    inc = db.get_incident(incident_id) or {}
    pol = json.loads(inc.get("policy_json") or "{}")
    who = lark_client.mention(actor, pol.get("check_person_name") or "") if actor else "the check person"
    _reply(incident_id, f"🟢 {inc.get('rule_name') or 'Alert'}: {who} checked and says it is normal (\"{text[:80]}\"). "
                        f"Incident #{incident_id} resolved, no page. If it fires again, a new incident opens.", in_thread=False)


def sweep_timeouts() -> int:
    """Called every SWEEP_INTERVAL_SEC. Returns how many incidents were paged."""
    n = 0
    for inc in db.due_waiting(time.time()):
        pol = json.loads(inc.get("policy_json") or "{}")
        page(int(inc["id"]), reason=f"No reply from check person within {pol.get('wait_minutes', '?')} min.", outcome="timeout_paged")
        n += 1
    return n


# --- paging ----------------------------------------------------------------------------

def page(incident_id: int, reason: str, outcome: str, actor: str = "bot") -> None:
    inc = db.get_incident(incident_id)
    if not inc or inc["status"] in ("paged", "resolved"):
        return
    alert = json.loads(inc.get("alert_json") or "{}")
    summary = f"[{inc.get('severity') or '?'}] {inc['rule_name']} ({alert.get('firing_count', 1)} instance(s))"
    attempts = int(inc.get("page_attempts") or 0) + 1
    db.update_incident(incident_id, page_attempts=attempts)
    try:
        pagerduty.trigger(
            inc["pd_dedup_key"], summary, inc.get("severity") or "",
            {
                "lark_thread": inc.get("thread_root_message_id") or "",
                "incident_id": incident_id,
                "reason": reason,
                "alert_value": alert.get("alert_value", ""),
                "alert_time": alert.get("alert_time", ""),
            },
        )
    except Exception as e:  # noqa: BLE001 — keep the incident alive; the sweeper retries in 60s
        log.exception("PagerDuty trigger failed for incident %s", incident_id)
        db.log_decision(incident_id, "pagerduty", "error", f"{type(e).__name__}: {e}"[:500])
        db.update_incident(incident_id, status="waiting", wait_deadline=time.time() + 60)
        if attempts == 1:
            _reply(incident_id, f"🚨 Could not reach PagerDuty ({type(e).__name__}). {reason}\nSomeone please call on-call manually. I will keep retrying.", in_thread=False)
        return
    db.update_incident(incident_id, status="paged", wait_deadline=None)
    db.log_outcome(incident_id, outcome, actor=actor, detail=reason)
    _reply(incident_id, f"📟 {inc['rule_name']}: paging on-call via PagerDuty. {reason}", in_thread=False)


def _resolve_from_monitor(incident_id: int) -> None:
    """MonitorFlow says the alert cleared."""
    inc = db.get_incident(incident_id)
    if not inc:
        return
    if inc["status"] == "paged":
        try:
            pagerduty.resolve(inc["pd_dedup_key"])
        except Exception as e:  # noqa: BLE001
            log.warning("PagerDuty resolve failed for incident %s: %s", incident_id, e)
    result = "monitor_resolved" if inc["status"] == "paged" else "self_resolved"
    db.update_incident(incident_id, status="resolved", wait_deadline=None, resolved_at=time.time())
    db.log_outcome(incident_id, result, actor="monitorflow")
    _reply(incident_id, f"🟢 {inc['rule_name']}: MonitorFlow reports the alert resolved. Incident #{incident_id} closed.", in_thread=False)


# --- PagerDuty status forwarded by Zoho Flow -------------------------------------------
#
# One Lark card per PagerDuty incident (table pd_cards). Zoho Flow forwards every PagerDuty
# event here; a button click (handle_card_action) repaints the same card right away and the
# PagerDuty event that follows a second later repaints it with the same state, so nothing doubles.

_PD_STATE = {  # PagerDuty event / status -> card state (cards.STYLE)
    "triggered": "triggered", "unacknowledged": "triggered", "reopened": "triggered",
    "escalated": "escalated",
    "acknowledged": "acknowledged",
    "snoozed": "snoozed",
    "resolved": "resolved",
}


def _pd_status(body: Dict[str, Any]) -> str:
    """'acknowledged' from {"status": "acknowledged"} or {"event": "incident.acknowledged"}."""
    raw = str(body.get("status") or body.get("event") or body.get("event_type") or "").lower().strip()
    return raw.split(".")[-1]


_PD_INCIDENT_ID = re.compile(r"^[PQ][A-Z0-9]{13}$")


def _pd_incident_id(body: Dict[str, Any]) -> str:
    """The PagerDuty incident id (Q..., 14 chars). Zoho's payload has two ids next to each other:
    Payload → Event → Id is the webhook *event* id (01H..., 26 chars) and gives 404 on every REST
    call; Payload → Event → Data → Id is the incident. If the wrong one was mapped, take the id from
    the end of html_url instead, like the old Deluge function did with url.getSuffix."""
    given = str(body.get("incident_id") or body.get("id") or "").strip()
    if _PD_INCIDENT_ID.match(given):
        return given
    url = str(body.get("html_url") or "")
    from_url = url.rstrip("/").rsplit("/incidents/", 1)[-1].split("?")[0] if "/incidents/" in url else ""
    if _PD_INCIDENT_ID.match(from_url):
        if given:
            log.warning("pd status: incident_id %r is not a PagerDuty incident id; using %s from html_url "
                        "(fix the Zoho chip: Payload → Event → Data → Id)", given, from_url)
        return from_url
    return given


def _summary_text(value: Any) -> str:
    """Zoho may send a plain string, a list of names, or PagerDuty's [{assignee:{summary}}] list."""
    if isinstance(value, list):
        names = []
        for v in value:
            if isinstance(v, dict):
                v = (v.get("assignee") or v).get("summary") or v.get("name") or ""
            if v:
                names.append(str(v))
        return ", ".join(names)
    return str(value or "")


def _push_card(pd_id: str, message_id: str, chat_id: str, card: Dict[str, Any]) -> None:
    """PATCH the existing card, or post it the first time and remember the message id."""
    try:
        if message_id:
            lark_client.update_card(message_id, card)
        else:
            data = lark_client.post_card(chat_id or config.FIXED_GROUP_CHAT_ID, card)
            db.upsert_pd_card(pd_id, lark_message_id=data.get("message_id") or None)
    except Exception as e:  # noqa: BLE001
        log.warning("pd card %s: Lark paint failed: %s", pd_id, e)


def _render_card(row: Dict[str, Any]) -> Dict[str, Any]:
    """The Lark card for a pd_cards row, in its current state, with history and the latest AI note."""
    history = json.loads(row.get("history_json") or "[]")
    notes = json.loads(row.get("ai_notes_json") or "{}")
    state = row.get("state") or "triggered"
    # show the newest note we have: the current state's, else the latest earlier state's (a note can
    # finish after the state already moved on, and 'snoozed' has no note of its own)
    note_stage = next((h["state"] for h in reversed(history) if notes.get(h["state"])), "")
    return cards.pd_incident_card(
        state, row["pd_incident_id"], row.get("title") or row["pd_incident_id"], row.get("html_url") or "",
        urgency=row.get("urgency") or "", service=row.get("service") or "", assignees=row.get("assignees") or "",
        history=[h["line"] for h in history], ai_note=notes.get(note_stage, ""), ai_note_stage=note_stage,
        meeting_url=row.get("meeting_url") or "",
    )


def _ai_annotate(pd_id: str, state: str) -> None:
    """Background: ask the model for a note on this state, store it, repaint the card. Any failure is
    logged and the card keeps working without a note. Never on the request path."""
    try:
        row = db.get_pd_card(pd_id) or {}
        inc = db.get_incident(int(row["incident_id"])) if row.get("incident_id") else None
        alert = json.loads(inc.get("alert_json") or "{}") if inc else {}
        ctx = {
            "title": row.get("title"), "service": row.get("service"), "urgency": row.get("urgency"),
            "assignees": row.get("assignees"),
            "timeline": [h["line"] for h in json.loads(row.get("history_json") or "[]")],
            "alert": alert,
            "past_cases": db.past_cases(inc["rule_name"]) if inc else [],
        }
        text = ai_notes.note(state, ctx)
        row = db.get_pd_card(pd_id) or row                      # re-read: the state may have moved on meanwhile
        notes = json.loads(row.get("ai_notes_json") or "{}")
        notes[state] = text
        row = db.upsert_pd_card(pd_id, ai_notes_json=json.dumps(notes, ensure_ascii=False))
        _push_card(pd_id, row.get("lark_message_id") or "", row.get("chat_id") or "", _render_card(row))
        if inc:
            db.log_decision(int(inc["id"]), "ai_note", state, text[:500])
            if state == "resolved":
                db.log_outcome(int(inc["id"]), "ai_summary", actor="ai", detail=text[:1000])
    except Exception as e:  # noqa: BLE001 — commentary only; never let it affect the incident
        log.warning("ai note for %s (%s) skipped: %s", pd_id, state, e)


def _paint_pd_card(pd_id: str, state: str, by: str = "", fields: Optional[Dict[str, Any]] = None,
                   message_id: str = "", minutes: int = 0, background: bool = False,
                   incident_id: Optional[int] = None, extra_line: str = "") -> Dict[str, Any]:
    """Record the new state and repaint (or first post) the one card for this PagerDuty incident.
    Returns the card dict so a callback can also hand it straight back to Lark. Lark errors are
    logged, never raised: the PagerDuty side already happened and must stay the source of truth.
    background=True pushes to Lark from a thread: a card callback must answer within 3 s
    (Lark error 200341 otherwise) and PagerDuty has already used most of that."""
    fields = fields or {}
    row = db.get_pd_card(pd_id) or {}
    history = json.loads(row.get("history_json") or "[]")
    last_state = history[-1]["state"] if history else ""
    # same state twice (duplicate event, or the event echoing a click) adds no line;
    # a snooze already acknowledged the incident, so the acknowledged event after it is noise
    changed = state != last_state and not (state == "acknowledged" and last_state == "snoozed")
    if changed:
        stamp = time.strftime("%H:%M")
        who = f" by {by}" if by else ""
        line = {
            "triggered": f"🔴 Triggered {stamp}",
            "escalated": f"🟠 Escalated{who} {stamp}",
            "acknowledged": f"🟡 Acknowledged{who} {stamp}",
            "snoozed": f"⏸ Ignored for {minutes} min{who} {stamp}. PagerDuty calls again after that unless resolved.",
            "resolved": f"🟢 Resolved{who} {stamp}",
        }.get(state, f"{state}{who} {stamp}")
        history = (history + [{"state": state, "line": line}])[-8:]
    if extra_line:                                             # e.g. a failed click; not a state change
        history = (history + [{"state": state, "line": f"{extra_line} {time.strftime('%H:%M')}"}])[-8:]

    row = db.upsert_pd_card(
        pd_id,
        state=state,
        lark_message_id=message_id or str(fields.get("lark_message_id") or "") or None,
        chat_id=str(fields.get("chat_id") or "") or None,
        title=str(fields.get("title") or "") or None,
        html_url=str(fields.get("html_url") or "") or None,
        urgency=str(fields.get("urgency") or "") or None,
        service=_summary_text(fields.get("service")) or None,
        assignees=_summary_text(fields.get("assignees")) or None,
        incident_id=incident_id,
        history_json=json.dumps(history, ensure_ascii=False),
    )
    card = _render_card(row)
    args = (pd_id, row.get("lark_message_id") or "", row.get("chat_id") or "", card)
    if background:
        threading.Thread(target=_push_card, args=args, daemon=True, name=f"paint-{pd_id}").start()
    else:
        _push_card(*args)
    # a new state gets an AI note, written in the background and painted onto the card when ready
    if changed and config.AI_NOTES and config.AI_BACKEND != "off" and state in ai_notes.STAGES:
        threading.Thread(target=_ai_annotate, args=(pd_id, state), daemon=True, name=f"ai-{pd_id}-{state}").start()
    if changed and state == "resolved" and row.get("meeting_reserve_id"):
        threading.Thread(target=_end_meeting, args=(pd_id,), daemon=True, name=f"paint-{pd_id}-meeting").start()
    return card


def _end_meeting(pd_id: str) -> None:
    """Incident resolved: cancel its Lark meeting reservation so the join link stops working."""
    row = db.get_pd_card(pd_id) or {}
    rid = row.get("meeting_reserve_id") or ""
    try:
        lark_client.delete_meeting(rid)
        db.upsert_pd_card(pd_id, meeting_reserve_id="")
        log.info("meeting reservation %s for %s cancelled", rid, pd_id)
    except Exception as e:  # noqa: BLE001
        log.warning("could not cancel meeting %s for %s: %s (expires on its own)", rid, pd_id, e)


def handle_pd_status(body: Dict[str, Any]) -> Dict[str, Any]:
    """Body from Zoho Flow after every PagerDuty event (all fields except incident_id + status optional):
      {incident_id, status | event, title, html_url, urgency, service, assignees, agent,
       incident_key | dedup_key, lark_message_id, chat_id}
    1) repaint (or first post) the one Lark card for that PagerDuty incident;
    2) if incident_key is one of our dedup keys, record the outcome on our incident too."""
    status = _pd_status(body)
    pd_id = _pd_incident_id(body)
    out: Dict[str, Any] = {"ok": True, "status": status, "pd_incident_id": pd_id}

    # 1) which incident of ours is this? (needed before painting so the card can carry our incident id)
    row = db.get_pd_card(pd_id) if pd_id else None
    dedup = str(body.get("incident_key") or body.get("dedup_key") or "")
    inc = db.find_incident_by_dedup(dedup) if dedup else None
    if not inc and row and row.get("incident_id"):
        inc = db.get_incident(int(row["incident_id"]))
    if not inc and pd_id and db.find_open_incident_any():
        # Zoho cannot send a usable key (PagerDuty's incident_key is null on alert-based services),
        # so read the incident's alerts: alert_key is our dedup_key, body.details.incident_id is our row
        try:
            for alert in pagerduty.get_incident_alerts(pd_id):
                dedup = str(alert.get("alert_key") or "")
                inc = db.find_incident_by_dedup(dedup) if dedup else None
                if not inc:
                    details = (alert.get("body") or {}).get("details") or {}
                    if str(details.get("incident_id") or "").isdigit():
                        inc = db.get_incident(int(details["incident_id"]))
                if inc:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("pd status: could not read alerts of %s from PagerDuty: %s", pd_id, e)

    # 2) paint (or first post) the one card for this PagerDuty incident.
    #    An id we have never seen must exist in PagerDuty: the status secret alone must not be enough
    #    to make the bot post cards for made-up incidents. (Network trouble is not a 404: we still paint.)
    if row is None and pd_id and status in _PD_STATE:
        try:
            real = pagerduty.get_incident(pd_id)
            for k_body, k_pd in (("title", "title"), ("html_url", "html_url"), ("urgency", "urgency"), ("service", "service")):
                if not body.get(k_body) and real.get(k_pd):      # fill gaps Zoho left from PagerDuty itself
                    body[k_body] = real[k_pd]
        except pagerduty.PagerDutyError as e:
            if "HTTP 404" in str(e):
                log.warning("pd status %s for unknown PagerDuty incident %s rejected (not found in PagerDuty)", status, pd_id)
                out["ok"], out["card"], out["incident_id"] = False, "rejected (incident not found in PagerDuty)", None
                return out
            log.warning("pd status: could not verify %s with PagerDuty (%s); painting anyway", pd_id, e)
        except Exception as e:  # noqa: BLE001
            log.warning("pd status: could not verify %s with PagerDuty (%s); painting anyway", pd_id, e)
    if status == "triggered" and row and row.get("state") == "resolved":
        # PagerDuty never re-triggers a resolved incident id (a new alert gets a new id and a new card),
        # so this is a late or replayed event. Only an explicit 'reopened' takes a card back to red.
        log.info("pd status triggered for resolved card %s ignored as stale", pd_id)
        out["card"] = "skipped (stale triggered after resolved)"
    elif pd_id and status in _PD_STATE:
        try:
            _paint_pd_card(pd_id, _PD_STATE[status], by=_summary_text(body.get("agent") or body.get("user")),
                           fields=body, incident_id=int(inc["id"]) if inc else None)
            out["card"] = "painted"
        except Exception as e:  # noqa: BLE001
            log.exception("pd card %s: %s failed", pd_id, status)
            out["card"] = f"error: {type(e).__name__}: {e}"[:200]
    else:
        out["card"] = "skipped"
        log.info("pd status %r for %r: no card change", status, pd_id)

    # 3) record the outcome on our incident
    if not inc:
        log.info("pd status %s for %s: incident_key %r matches none of our incidents (fine for manual tests)", status, pd_id, dedup)
        out["incident_id"] = None
        return out
    iid = int(inc["id"])
    db.log_outcome(iid, f"pd_{status or 'unknown'}", actor=_summary_text(body.get("agent") or body.get("user")),
                   detail=json.dumps(body, ensure_ascii=False)[:1000])
    if status == "resolved" and inc["status"] != "resolved":
        db.update_incident(iid, status="resolved", wait_deadline=None, resolved_at=time.time())
    if config.POST_PD_STATUS_TO_THREAD:
        _reply(iid, f"PagerDuty: {status} {_summary_text(body.get('agent') or body.get('user'))}".strip())
    out["incident_id"] = iid
    return out


def handle_card_action(event: Dict[str, Any]) -> Dict[str, Any]:
    """A button or dropdown on a Lark card (card.action.trigger). Lark gives the callback 3 s in total,
    network included (Lark → ngrok → here → PagerDuty), and the first version that waited for PagerDuty
    inside the callback hit error 200341 most of the time. So: validate, answer at once with a toast,
    and do PagerDuty + the card repaint from a thread (_do_card_action). The card still only changes
    after PagerDuty confirmed; it just changes a second after the click instead of inside the callback."""
    act = event.get("action") or {}
    value = act.get("value") or {}
    action = str(value.get("action") or "")
    incident_id = str(value.get("incident_id") or "")
    who = str((event.get("operator") or {}).get("open_id") or "")
    clicked_message_id = str((event.get("context") or {}).get("open_message_id") or "")
    if not incident_id or action not in ("acknowledged", "resolved", "snooze", "meeting"):
        log.warning("card click ignored: value=%s", value)
        return {"toast": {"type": "error", "content": "Unknown button"}}
    minutes = int(act.get("option") or 0) if action == "snooze" else 0   # the dropdown choice, in minutes
    if action == "meeting":
        threading.Thread(target=_do_meeting, args=(incident_id, who, clicked_message_id),
                         daemon=True, name=f"act-{incident_id}").start()
        return {"toast": {"type": "info", "content": "Creating the incident meeting…"}}
    threading.Thread(target=_do_card_action, args=(action, incident_id, who, minutes, clicked_message_id),
                     daemon=True, name=f"act-{incident_id}").start()
    label = {"acknowledged": "Acknowledging", "resolved": "Resolving", "snooze": f"Ignoring for {minutes} min"}[action]
    return {"toast": {"type": "info", "content": f"{label} in PagerDuty…"}}


def _do_meeting(incident_id: str, who: str, clicked_message_id: str) -> None:
    """Background half of "Create meeting": one Lark video meeting per PagerDuty incident, owned by the
    person who clicked. Not a paging decision, so PagerDuty is not involved. A second click (or a race
    between two clickers) reuses the first link. Failure → a warning line on the card, state unchanged."""
    by = f"<at id={who}></at>" if who else "someone"
    row = db.get_pd_card(incident_id)
    if not row:
        log.warning("meeting: no card for %s, ignored", incident_id)
        return
    state = row.get("state") or "triggered"
    if row.get("meeting_url"):
        log.info("meeting for %s already exists; repainting", incident_id)
        _paint_pd_card(incident_id, state, message_id=clicked_message_id)
        return
    try:
        reserve = lark_client.create_meeting(row.get("title") or incident_id, who, config.MEETING_HOURS)
    except Exception as e:  # noqa: BLE001
        log.warning("meeting for %s by %s failed: %s", incident_id, who, e)
        _paint_pd_card(incident_id, state, message_id=clicked_message_id,
                       extra_line=f"⚠️ Meeting by {by} failed: {str(e)[:100]}")
        return
    row = db.get_pd_card(incident_id) or row
    if row.get("meeting_url"):                                  # someone else won the race
        _paint_pd_card(incident_id, state, message_id=clicked_message_id)
        return
    db.upsert_pd_card(incident_id, meeting_url=reserve["url"], meeting_reserve_id=str(reserve.get("id") or "") or None)
    log.info("meeting for %s created by %s: %s", incident_id, who, reserve["url"])
    _paint_pd_card(incident_id, state, message_id=clicked_message_id, extra_line=f"📞 Meeting created by {by}")
    # Also a message in the group: Lark attaches the live meeting widget (meeting id, who is in, Ended)
    # to a meeting link in a top-level rich-text message, which a card cannot show. Same form as the P0 bot:
    # title line, then the link rendered with the meeting's name.
    title = row.get("title") or incident_id
    try:
        lark_client.post_rich(
            row.get("chat_id") or config.FIXED_GROUP_CHAT_ID, title,
            [[{"tag": "text", "text": "🚨 Incident meeting created."}],
             [{"tag": "text", "text": "join in meeting link"}],
             [{"tag": "a", "text": f"Video meeting—{title}", "href": reserve["url"]}]],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("meeting message for %s failed: %s (link is on the card)", incident_id, e)
    if row.get("incident_id"):
        db.log_decision(int(row["incident_id"]), "reply", "meeting", f"{who} {reserve['url']}")


def _do_card_action(action: str, incident_id: str, who: str, minutes: int, clicked_message_id: str) -> None:
    """Background half of a card click: PagerDuty first, then repaint the card in the confirmed state.
    If PagerDuty refuses, the card gets a one-line warning instead (a toast is no longer possible),
    except for 'already acknowledged/resolved', which is just a double click."""
    by = f"<at id={who}></at>" if who else "someone"      # card (lark_md) mention syntax differs from text messages
    t0 = time.time()
    try:
        if action == "snooze":
            inc = pagerduty.snooze(incident_id, minutes)
            state = "snoozed"
        else:
            inc = pagerduty.set_status(incident_id, action)
            state = action
        log.info("card: %s %s on %s confirmed by PagerDuty in %.1fs", who, action, incident_id, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if "already" in err.lower():
            log.info("card: %s on %s by %s: %s (double click, ignored)", action, incident_id, who, err[:80])
            return
        log.warning("card: %s on %s by %s failed: %s", action, incident_id, who, err)
        row = db.get_pd_card(incident_id)
        if row:
            _paint_pd_card(incident_id, row.get("state") or "triggered", message_id=clicked_message_id,
                           extra_line=f"⚠️ {action.capitalize()} by {by} failed: {err[:100]}")
        return

    fields = {
        "title": inc.get("title"), "html_url": inc.get("html_url"), "urgency": inc.get("urgency"),
        "service": inc.get("service"), "assignees": inc.get("assignments"),
    }
    _paint_pd_card(incident_id, state, by=by, fields=fields, message_id=clicked_message_id, minutes=minutes)


# --- helpers ----------------------------------------------------------------------------

def _reply(incident_id: int, text: str, in_thread: bool = True) -> None:
    """in_thread=True: reply inside the incident's topic thread. in_thread=False: a new top-level
    message in the group chat, so everyone sees it without opening the thread (Lark keeps any reply
    to a topic root inside the topic, even with reply_in_thread=false, verified 2026-11-11).
    A Lark failure must never stop the pipeline."""
    inc = db.get_incident(incident_id) or {}
    try:
        if in_thread:
            root = inc.get("thread_root_message_id") or ""
            if not root:
                log.warning("incident %s has no thread root; wanted to say: %s", incident_id, text)
                return
            lark_client.reply_text(root, text, in_thread=True)
        else:
            chat = inc.get("chat_id") or config.FIXED_GROUP_CHAT_ID or config.ALERT_CHAT_ID
            lark_client.post_text(chat, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Lark reply failed for incident %s: %s", incident_id, e)
