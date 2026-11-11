"""PagerDuty Events API v2. We only send trigger / resolve; Jeno owns services,
schedules and who gets called. Status comes back through Zoho Flow."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from . import config

log = logging.getLogger("alert-ir")


class PagerDutyError(RuntimeError):
    pass


# PagerDuty only accepts critical | error | warning | info. Unknown = critical (safe side).
SEVERITY_MAP = {
    "p0": "critical", "critical": "critical", "disaster": "critical",
    "p1": "error", "error": "error", "high": "error",
    "p2": "warning", "warning": "warning", "medium": "warning",
    "p3": "info", "info": "info", "low": "info",
}


def _send(event_action: str, dedup_key: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"routing_key": config.PD_ROUTING_KEY, "event_action": event_action, "dedup_key": dedup_key}
    if payload:
        body["payload"] = payload
    if config.DRY_RUN:
        log.info("[DRY_RUN] pagerduty %s dedup=%s payload=%s", event_action, dedup_key, payload)
        return {"status": "success", "dedup_key": dedup_key}
    if not config.PD_ROUTING_KEY:
        raise PagerDutyError("PD_ROUTING_KEY is empty")
    r = requests.post(config.PD_EVENTS_URL, json=body, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 202:
        raise PagerDutyError(f"{event_action}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def trigger(dedup_key: str, summary: str, severity: str, custom_details: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "summary": summary[:1024],
        "source": "monitorflow",
        "severity": SEVERITY_MAP.get((severity or "").lower(), "critical"),
        "custom_details": custom_details,   # lark_thread lives here so Zoho Flow can find the thread
    }
    return _send("trigger", dedup_key, payload)


def resolve(dedup_key: str) -> Dict[str, Any]:
    return _send("resolve", dedup_key)


def acknowledge(dedup_key: str) -> Dict[str, Any]:
    """Events API acknowledge on our own dedup key: used right after trigger() when someone replied
    'acknowledge' in Lark, so the incident exists in PagerDuty but is already owned."""
    return _send("acknowledge", dedup_key)

def get_incident(incident_id: str) -> Dict[str, Any]:
    """Read one incident (REST). Used to confirm an id exists before the bot paints a card for an
    incident it has never seen, so a forged /pagerduty/status call cannot create fake cards.
    Raises PagerDutyError with 'HTTP 404' for an unknown id."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] pagerduty get_incident %s", incident_id)
        return {"id": incident_id}
    if not config.PD_API_KEY:
        raise PagerDutyError("PD_API_KEY not set")
    r = requests.get(
        f"https://api.pagerduty.com/incidents/{incident_id}",
        headers={"Authorization": f"Token token={config.PD_API_KEY}", "Accept": "application/vnd.pagerduty+json;version=2"},
        timeout=config.HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise PagerDutyError(f"get_incident: HTTP {r.status_code} {r.text[:300]}")
    return r.json()["incident"]


def get_incident_alerts(incident_id: str) -> List[Dict[str, Any]]:
    """The alerts behind a PagerDuty incident (REST). This is where our link back lives: on a service
    with alerts enabled the incident's own `incident_key` is null (verified 2026-11-09), while each
    alert has `alert_key` = the dedup_key we sent and `body.details` = our custom_details
    (incident_id, lark_thread). Used when Zoho's payload cannot tell us which incident of ours it is."""
    if config.DRY_RUN:
        log.info("[DRY_RUN] pagerduty get_incident_alerts %s", incident_id)
        return []
    if not config.PD_API_KEY:
        raise PagerDutyError("PD_API_KEY not set")
    r = requests.get(
        f"https://api.pagerduty.com/incidents/{incident_id}/alerts",
        headers={"Authorization": f"Token token={config.PD_API_KEY}", "Accept": "application/vnd.pagerduty+json;version=2"},
        timeout=config.HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise PagerDutyError(f"get_incident_alerts: HTTP {r.status_code} {r.text[:300]}")
    return list(r.json().get("alerts") or [])


def set_status(incident_id: str, status: str) -> Dict[str, Any]:
    """Acknowledge or resolve through the REST API. Used by the Lark card buttons."""
    if status not in ("acknowledged", "resolved"):
        raise PagerDutyError(f"bad status {status}")
    if config.DRY_RUN:
        log.info("[DRY_RUN] pagerduty set_status %s -> %s", incident_id, status)
        return {"id": incident_id, "status": status}
    if not config.PD_API_KEY or not config.PD_FROM_EMAIL:
        raise PagerDutyError("PD_API_KEY / PD_FROM_EMAIL not set")
    r = requests.put(
        f"https://api.pagerduty.com/incidents/{incident_id}",
        headers={
            "Authorization": f"Token token={config.PD_API_KEY}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
            "From": config.PD_FROM_EMAIL,
        },
        json={"incident": {"type": "incident_reference", "status": status}},
        timeout=config.HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise PagerDutyError(f"set_status: HTTP {r.status_code} {r.text[:300]}")
    return r.json()["incident"]

def snooze(incident_id: str, minutes: int) -> Dict[str, Any]:
    """'Ignore for N minutes' from the Lark card. PagerDuty only snoozes acknowledged incidents,
    so acknowledge first; when the time is up PagerDuty re-triggers and calls again."""
    if minutes < 1 or minutes > 24 * 60:
        raise PagerDutyError(f"bad snooze {minutes} min")
    if config.DRY_RUN:
        log.info("[DRY_RUN] pagerduty snooze %s for %s min", incident_id, minutes)
        return {"id": incident_id, "status": "acknowledged"}
    set_status(incident_id, "acknowledged")
    r = requests.post(
        f"https://api.pagerduty.com/incidents/{incident_id}/snooze",
        headers={
            "Authorization": f"Token token={config.PD_API_KEY}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
            "From": config.PD_FROM_EMAIL,
        },
        json={"duration": minutes * 60},
        timeout=config.HTTP_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise PagerDutyError(f"snooze: HTTP {r.status_code} {r.text[:300]}")
    return r.json()["incident"]
