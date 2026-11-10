"""The one Lark card we keep per PagerDuty incident, in every state.

    state          header   buttons
    triggered      red      Acknowledge · Resolve · Ignore for… · Open in PagerDuty
    escalated      orange   Acknowledge · Resolve · Ignore for… · Open in PagerDuty
    acknowledged   yellow   Resolve · Open in PagerDuty
    snoozed        yellow   Resolve · Open in PagerDuty
    resolved       green    Open in PagerDuty

Button values round-trip through Lark's card.action.trigger callback to pipeline.handle_card_action:
{"action": "acknowledged" | "resolved" | "snooze", "incident_id": "Q..."}; the Ignore dropdown adds
`option` = minutes. Keep this the only place that knows what the card looks like.
"""
from __future__ import annotations

from typing import Any, Dict, List

STYLE: Dict[str, Dict[str, Any]] = {
    "triggered":    {"color": "red",    "buttons": ["ack", "resolve", "ignore"]},
    "escalated":    {"color": "orange", "buttons": ["ack", "resolve", "ignore"]},
    "acknowledged": {"color": "yellow", "buttons": ["resolve"]},
    "snoozed":      {"color": "yellow", "buttons": ["resolve"]},
    "resolved":     {"color": "green",  "buttons": []},
}

IGNORE_OPTIONS = [(5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"),
                  (240, "4 hours"), (720, "12 hours"), (1440, "24 hours")]


def _plain(text: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": text}


def _button(label: str, action: str, incident_id: str, kind: str) -> Dict[str, Any]:
    return {"tag": "button", "text": _plain(label), "type": kind, "value": {"action": action, "incident_id": incident_id}}


def _ignore_select(incident_id: str) -> Dict[str, Any]:
    return {
        "tag": "select_static",
        "placeholder": _plain("Ignore for..."),
        "value": {"action": "snooze", "incident_id": incident_id},
        "options": [{"text": _plain(label), "value": str(minutes)} for minutes, label in IGNORE_OPTIONS],
    }


def pd_incident_card(state: str, incident_id: str, title: str, html_url: str,
                     urgency: str = "", service: str = "", assignees: str = "",
                     history: List[str] | None = None, ai_note: str = "", ai_note_stage: str = "") -> Dict[str, Any]:
    style = STYLE.get(state) or STYLE["triggered"]
    facts = [f"**Urgency:** {urgency or '?'} · **Service:** {service or '?'}"]
    if assignees:
        facts.append(f"**Assigned:** {assignees}")
    actions: List[Dict[str, Any]] = []
    for b in style["buttons"]:
        if b == "ack":
            actions.append(_button("Acknowledge", "acknowledged", incident_id, "primary"))
        elif b == "resolve":
            actions.append(_button("Resolve", "resolved", incident_id, "default"))
        elif b == "ignore":
            actions.append(_ignore_select(incident_id))
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
