"""The one Lark card we keep per PagerDuty incident, in every state.

    state          header   buttons (2026-11-12)
    triggered      red      Acknowledge · Resolve / Normal · 📞 Call on duty · Open in PagerDuty
    acknowledged   yellow   Escalate · Resolve / Normal · 📞 Need to call duty? · Open in PagerDuty
    snoozed        yellow   same as acknowledged
    escalated      orange   Resolve / Normal · 📞 Need to call duty? · Open in PagerDuty
    resolved       green    Open in PagerDuty

"Resolve / Normal" resolves in PagerDuty and, for a card linked to one of our alerts, records outcome
human_normal (what triage learns false alarms from).

"Call on duty" opens the incident meeting (owner = clicker) and rings the duty person into it once it is
live; its join link shows on the card until resolved (the bot cancels the reservation then). "Escalate"
moves the PagerDuty incident to the next escalation level. Button values round-trip through Lark's
card.action.trigger callback to pipeline.handle_card_action:
{"action": "acknowledged" | "resolved" | "snooze" | "meeting" | "escalate" | "call_duty", "incident_id": "Q..."};
the (now unused) Ignore dropdown adds `option` = minutes. Keep this the only place that knows what the card looks like.
"""
from __future__ import annotations

from typing import Any, Dict, List

# Renzel 2026-11-12. "snoozed" (Ignore, now only from the PagerDuty app) keeps the yellow buttons.
STYLE: Dict[str, Dict[str, Any]] = {
    "triggered":    {"color": "red",    "buttons": ["ack", "resolve", "call_duty"]},
    "acknowledged": {"color": "yellow", "buttons": ["escalate", "resolve", "call_duty?"]},
    "snoozed":      {"color": "yellow", "buttons": ["escalate", "resolve", "call_duty?"]},
    "escalated":    {"color": "orange", "buttons": ["resolve", "call_duty?"]},
    "resolved":     {"color": "green",  "buttons": []},
}

IGNORE_OPTIONS = [(5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"),
                  (240, "4 hours"), (720, "12 hours"), (1440, "24 hours")]


def _plain(text: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _button(label: str, action: str, incident_id: str, btype: str, **extra: str) -> Dict[str, Any]:
    return {"tag": "button", "text": _plain(label), "type": btype,
            "value": {"action": action, "incident_id": incident_id, **extra}}


# --- the 🟡 prompt card (the "trigger decision": check person tagged, clock running) -------------

def prompt_card(incident_id: int, rule_name: str, severity: str, what: str, check_person_open_id: str,
                clock_text: str = "") -> Dict[str, Any]:
    """The 🟡 prompt as a card so it can carry "📞 Call check person" (Lark urgent phone call, no meeting).
    Only for alerts that wait for the check person; alerts that page at once get the PagerDuty card only."""
    iid = str(incident_id)
    mention = f"<at id={check_person_open_id}></at>" if check_person_open_id else "check person"
    body = (f"🟡 **{rule_name}** ({severity or '?'}): {what}\n"
            f"{mention} please check and reply to this message: 'normal' if it is fine, or 'acknowledge' "
            f"if you are looking into it. {clock_text}")
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "action", "actions": [
                _button("📞 Call check person", "call_cp", iid, "primary", kind="incident"),
            ]},
        ],
    }


# --- the SRE escalation card ("Escalate" pressed on a PagerDuty card) ------------------------

def sre_escalation_card(title: str, by_open_id: str, failed: str, summary: str, recording_url: str,
                        pd_url: str, provider: str = "", game: str = "") -> Dict[str, Any]:
    """What the SRE group gets when someone escalates: the facts and two links. The recording itself
    is posted as a video reply under this card."""
    who = f"<at id={by_open_id}></at>" if by_open_id else "someone"
    lines = [f"**Escalated by** {who}"]
    if provider or game:
        lines.append(f"**Provider:** {provider or '?'}" + (f" · **Game:** {game}" if game else ""))
    if failed:
        lines.append(f"**Failed:** {failed}")
    elements: List[Dict[str, Any]] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    if summary:
        elements += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": summary.strip()}}]
    actions: List[Dict[str, Any]] = []
    if recording_url:
        actions.append({"tag": "button", "text": _plain("▶ Watch recording"), "type": "primary", "url": recording_url})
    if pd_url:
        actions.append({"tag": "button", "text": _plain("Open in PagerDuty"), "type": "default", "url": pd_url})
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red", "title": _plain(f"🚨 {title}")},
        "elements": elements,
    }


# --- the bot's own incident card (before PagerDuty is involved) ---------------------------
#
#   state      header   buttons
#   checking   yellow   Resolve · Create meeting · Page on-call now      (posted when someone acknowledges)
#   paged      red      none  (the PagerDuty card takes over)
#   resolved   green    none
# Button values carry kind="incident" so pipeline.handle_card_action can tell them from PagerDuty cards.

INCIDENT_STYLE: Dict[str, Dict[str, Any]] = {
    "checking": {"color": "yellow", "buttons": ["resolve", "meeting", "page"]},
    "paged":    {"color": "red",    "buttons": []},
    "resolved": {"color": "green",  "buttons": []},
}


def incident_card(incident_id: int, state: str, rule_name: str, severity: str = "", firing_count: int = 1,
                  deadline_text: str = "", history: List[str] | None = None, meeting_url: str = "",
                  ai_note: str = "", ai_note_stage: str = "") -> Dict[str, Any]:
    style = INCIDENT_STYLE.get(state) or INCIDENT_STYLE["checking"]
    iid = str(incident_id)
    facts = [f"**Severity:** {severity or '?'} · **Instances:** {firing_count} · **Incident:** #{iid}"]
    if deadline_text and state == "checking":
        facts.append(f"⏱ {deadline_text}")
    if meeting_url and state != "resolved":
        facts.append(f"📞 **Meeting:** [Join the incident call]({meeting_url})")
    actions: List[Dict[str, Any]] = []
    for b in style["buttons"]:
        if b == "resolve":
            actions.append(_button("Resolve", "bot_resolve", iid, "primary", kind="incident"))
        elif b == "meeting" and not meeting_url:
            actions.append(_button("Create meeting", "meeting", iid, "default", kind="incident"))
        elif b == "page":
            actions.append(_button("Page on-call now", "bot_page", iid, "danger", kind="incident"))
    elements: List[Dict[str, Any]] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(facts)}}]
    if history:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(history)}})
    if ai_note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"🤖 **AI note ({ai_note_stage or state})**\n{ai_note}"}})
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": style["color"], "title": _plain(rule_name or f"Incident #{iid}")},
        "elements": elements,
    }


def _ignore_select(incident_id: str) -> Dict[str, Any]:
    return {
        "tag": "select_static",
        "placeholder": _plain("Ignore for..."),
        "value": {"action": "snooze", "incident_id": incident_id},
        "options": [{"text": _plain(label), "value": str(minutes)} for minutes, label in IGNORE_OPTIONS],
    }


def pd_incident_card(state: str, incident_id: str, title: str, html_url: str,
                     urgency: str = "", service: str = "", assignees: str = "",
                     history: List[str] | None = None, ai_note: str = "", ai_note_stage: str = "",
                     meeting_url: str = "") -> Dict[str, Any]:
    style = STYLE.get(state) or STYLE["triggered"]
    facts = [f"**Urgency:** {urgency or '?'} · **Service:** {service or '?'}"]
    if assignees:
        facts.append(f"**Assigned:** {assignees}")
    if meeting_url and state != "resolved":        # resolved: the reservation is cancelled, no dead link
        facts.append(f"📞 **Meeting:** [Join the incident call]({meeting_url})")
    actions: List[Dict[str, Any]] = []
    for b in style["buttons"]:
        if b == "ack":
            actions.append(_button("Acknowledge", "acknowledged", incident_id, "primary"))
        elif b == "resolve":
            actions.append(_button("Resolve / Normal", "resolved", incident_id, "default"))
        elif b == "ignore":
            actions.append(_ignore_select(incident_id))
        elif b == "meeting" and not meeting_url:
            actions.append(_button("Create meeting", "meeting", incident_id, "default"))
        elif b == "escalate":
            actions.append(_button("Escalate", "escalate", incident_id, "danger"))
        elif b == "call_duty":
            actions.append(_button("📞 Call on duty", "call_duty", incident_id, "danger"))
        elif b == "call_duty?":
            actions.append(_button("📞 Need to call duty?", "call_duty", incident_id, "default"))
    actions.append({"tag": "button", "text": _plain("Open in PagerDuty"), "type": "default", "url": html_url or "https://app.pagerduty.com"})

    elements: List[Dict[str, Any]] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(facts)}}]
    if history:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(history)}})
    if ai_note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"🤖 **AI note ({ai_note_stage or state})**\n{ai_note}"}})
    elements.append({"tag": "action", "actions": actions})
    return {
        # update_multi: one shared card for everyone in the group, so a repaint (callback response
        # or PATCH) changes what every member sees, not only the person who clicked
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": style["color"], "title": _plain(title or incident_id)},
        "elements": elements,
    }
