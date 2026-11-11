"""Alert policy: which alert classes always page, who the check person is, how long
to wait for them. Source for now: policy.yaml (Lark Base later). The file is
re-read whenever it changes, so edits apply without a restart."""
from __future__ import annotations

import os
import re
import threading
from dataclasses import asdict, dataclass
from typing import Any, Dict

import yaml

from . import config


@dataclass
class Policy:
    always_page: bool = False
    check_person: str = ""            # Lark open_id (ou_...)
    check_person_name: str = ""
    wait_minutes: int = 0             # no acknowledgement within N min of the alert → page; 0 = no such deadline
    firing_minutes: int = 20          # still open N min after the alert started (acknowledged or not) → page
    matched: str = "default"          # which rule matched (for the audit trail)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_state: Dict[str, Any] = {"mtime": -1.0, "doc": {}}
_lock = threading.Lock()


def _doc() -> Dict[str, Any]:
    with _lock:
        try:
            mtime = os.path.getmtime(config.POLICY_PATH)
        except OSError:
            return {}
        if mtime != _state["mtime"]:
            with open(config.POLICY_PATH, encoding="utf-8") as f:
                _state["doc"] = yaml.safe_load(f) or {}
            _state["mtime"] = mtime
        return dict(_state["doc"])


def lookup(rule_name: str, severity: str) -> Policy:
    doc = _doc()
    d = doc.get("defaults") or {}
    p = Policy(
        always_page=bool(d.get("always_page", False)),
        check_person=str(d.get("check_person") or ""),
        check_person_name=str(d.get("check_person_name") or ""),
        wait_minutes=int(d.get("wait_minutes", config.DEFAULT_WAIT_MINUTES) or 0),
        firing_minutes=int(d.get("firing_minutes") or config.DEFAULT_FIRING_MINUTES),
    )
    keys = ("always_page", "check_person", "check_person_name", "wait_minutes", "firing_minutes")
    # first matching rule wins
    for rule in doc.get("rules") or []:
        pat = str(rule.get("match") or "")
        if pat and re.search(pat, rule_name, re.I):
            for k in keys:
                if k in rule:
                    setattr(p, k, rule[k])
            p.matched = pat
            break
    # severity overrides come after the name rules: a high threshold gets its own deadlines
    overrides = {str(k).lower(): v for k, v in (doc.get("severity_overrides") or {}).items()}
    sev = (severity or "").lower()
    if sev and isinstance(overrides.get(sev), dict):
        for k in keys:
            if k in overrides[sev]:
                setattr(p, k, overrides[sev][k])
        p.matched = f"{p.matched}+severity:{severity}"
    # severity list is a hard floor: a rule can't turn it off
    sev_always = [str(s).lower() for s in (doc.get("severity_always_page") or [])]
    if sev and sev in sev_always:
        p.always_page = True
        p.matched = f"severity:{severity}"
    p.always_page = bool(p.always_page)
    p.wait_minutes = int(p.wait_minutes or 0)
    p.firing_minutes = int(p.firing_minutes or config.DEFAULT_FIRING_MINUTES)
    return p
