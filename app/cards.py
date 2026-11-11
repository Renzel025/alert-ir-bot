"""The one Lark card we keep per PagerDuty incident, in every state.

    state          header   buttons
    triggered      red      Acknowledge · Resolve · Ignore for… · Open in PagerDuty
    escalated      orange   Acknowledge · Resolve · Ignore for… · Open in PagerDuty
    acknowledged   yellow   Resolve · Create meeting · Open in PagerDuty
    snoozed        yellow   Resolve · Create meeting · Open in PagerDuty
    resolved       green    Open in PagerDuty

"Create meeting" shows only while no meeting exists; once one does, the card shows its join link
instead until resolved (the bot cancels the reservation then, so the link is dropped). Button values round-trip through Lark's
card.action.trigger callback to pipeline.handle_card_action:
{"action": "acknowledged" | "resolved" | "snooze" | "meeting", "incident_id": "Q..."}; the Ignore
dropdown adds `option` = minutes. Keep this the only place that knows what the card looks like.
"""
from __future__ import annotations

from typing import Any, Dict, List

STYLE: Dict[str, Dict[str, Any]] = {
    "triggered":    {"color": "red",    "buttons": ["ack", "resolve", "ignore"]},
    "escalated":    {"color": "orange", "buttons": ["ack", "resolve", "ignore"]},
    "acknowledged": {"color": "yellow", "buttons": ["resolve", "meeting"]},
    "snoozed":      {"color": "yellow", "buttons": ["resolve", "meeting"]},
    "resolved":     {"color": "green",  "buttons": []},
}

IGNORE_OPTIONS = [(5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"),
                  (240, "4 hours"), (720, "12 hours"), (1440, "24 hours")]


def _plain(text: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _button(label: str, action: str, incident_id: str, btype: str, **extra: str) -> Dict[str, Any]:
    return {"tag": "button", "text": _plain(label), "type": btype,
            "value": {"action": action, "incident_id": incident_id, **extra}}


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
                  deadline_text: str = "", history: List[str] | None = None, meeting_url: str = "") -> Dict[str, Any]:
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
            actions.append(_button("Resolve", "resolved", incident_id, "default"))
        elif b == "ignore":
            actions.append(_ignore_select(incident_id))
        elif b == "meeting" and not meeting_url:
            actions.append(_button("Create meeting", "meeting", incident_id, "default"))
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
