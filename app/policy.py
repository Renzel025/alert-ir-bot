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
    wait_minutes: int = 5
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
        wait_minutes=int(d.get("wait_minutes") or config.DEFAULT_WAIT_MINUTES),
    )
    # first matching rule wins
    for rule in doc.get("rules") or []:
        pat = str(rule.get("match") or "")
        if pat and re.search(pat, rule_name, re.I):
            for k in ("always_page", "check_person", "check_person_name", "wait_minutes"):
                if k in rule:
                    setattr(p, k, rule[k])
            p.matched = pat
            break
    # severity list is a hard floor: a rule can't turn it off
    sev_always = [str(s).lower() for s in (doc.get("severity_always_page") or [])]
    if severity and severity.lower() in sev_always:
        p.always_page = True
        p.matched = f"severity:{severity}"
    p.always_page = bool(p.always_page)
    p.wait_minutes = int(p.wait_minutes)
    return p
