"""Turn a MonitorFlow message (posted in the Lark alert group by the same Lark app the bot uses) into an Alert.

MonitorFlow posts Lark *cards* (msg_type "interactive"); its real alert card has NOT been seen yet (only the channel test).
The card format below (2026-11-09) is from Renzel's own threshold alerting, which posts into the same group through the
same app; it is parsed as an alert for now. Add MonitorFlow's title/rows here once a real one arrives.

    title:  🚨 CRITICAL - Renzel-test
    row 1:  🔴  Severity: CRITICAL   🕐 Time: 2026-11-09 11:34:06
    row 2:  ENV: PROD   Domain: PLATFORM   Platform: Aliyun
    row 3:  providerId  ratio  3079  0.2282…          (a table: labels first, then one value per label per instance)
    row 4:  🙋 Report wrong alert click👆

Other automations post through the same app into the same group ("⚠️ Anomaly detected - …" from lark-alert-listener,
"Web/PC automatic testing for provider: …", the bot's own PagerDuty cards from Zoho). Those must parse to None.
The recovery card has NOT been seen yet; `_CARD_RESOLVED` is a guess until MonitorFlow shows one.

`parse()` still accepts the plain-text template (`[FIRING:n] rule_name` …) used by scripts/simulate.sh.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

_HEADER = re.compile(r"^\[(?P<status>FIRING|RESOLVED)(?::(?P<count>\d+))?\]\s*(?P<rule>.+?)\s*$", re.I)
_KV = re.compile(r"([A-Za-z_][\w.]*)\s*[:=]\s*([^,;|]+)")

_SEVERITIES = r"CRITICAL|HIGH|WARNING|WARN|MEDIUM|LOW|INFO|P[0-4]"
_CARD_TITLE = re.compile(r"^\W*(?P<sev>" + _SEVERITIES + r")\s*[-–—:]\s*(?P<rule>.+?)\s*$", re.I)
_CARD_RESOLVED = re.compile(r"^\W*(?P<status>RESOLVED|RECOVERED|RECOVERY)\s*[-–—:]\s*(?P<rule>.+?)\s*$", re.I)
_CHANNEL_TEST = "monitorflow channel test"
_KEY_TOKEN = re.compile(r"^[A-Za-z][\w /]{0,30}:$")                 # "Severity:"  "ENV:"
_INLINE_KV = re.compile(r"^([A-Za-z][\w /]{0,30}):\s*(.+)$")        # "Severity: CRITICAL" in one run
_NUMERIC = re.compile(r"^-?\d[\d.,%]*$")
_NO_WORD = re.compile(r"^[\W_]+$")                                  # emoji-only runs like "🔴 "


@dataclass
class Alert:
    rule_name: str
    status: str = "firing"            # firing | resolved
    severity: str = ""
    firing_count: int = 1
    alert_time: str = ""
    alert_value: str = ""
    instances: List[Dict[str, str]] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)   # ENV / Domain / Platform … from the card
    raw_text: str = ""

    @property
    def is_test(self) -> bool:
        return "channel test" in self.rule_name.lower()

    @property
    def fingerprint(self) -> str:
        # Same rule = same incident while it is open. Revisit if one rule covers many providers.
        return re.sub(r"\s+", "_", self.rule_name.strip().lower())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_text(message_type: str, content: str) -> str:
    """Plain text from a Lark message `content` string (text, post or card)."""
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return content or ""
    if message_type == "text":
        return str(data.get("text") or "")
    # post / interactive: collect every text-like string in document order
    out: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "content", "title") and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return "\n".join(out)


# --- MonitorFlow card ---------------------------------------------------------------------

def _card_title(data: Dict[str, Any]) -> str:
    title = data.get("title")
    if isinstance(title, str):
        return title.strip()
    header = data.get("header") or {}
    if isinstance(header, dict):
        t = header.get("title")
        if isinstance(t, dict):
            return str(t.get("content") or "").strip()
        if isinstance(t, str):
            return t.strip()
    return ""


def _row_tokens(row: Any) -> List[str]:
    """Text runs of one card row, split on newlines, emoji-only runs dropped. img/hr/button/note are skipped."""
    out: List[str] = []
    elements = row if isinstance(row, list) else [row]
    for el in elements:
        if not isinstance(el, dict) or el.get("tag") not in ("text", "lark_md", "plain_text", "div"):
            continue
        txt = el.get("text")
        if isinstance(txt, dict):
            txt = txt.get("content")
        if not isinstance(txt, str):
            continue
        for part in txt.split("\n"):
            part = part.strip()
            if part and not _NO_WORD.match(part):
                out.append(part)
    return out


def _table(tokens: List[str]) -> List[Dict[str, str]]:
    """['providerId','ratio','3079','0.22'] -> [{'providerId':'3079','ratio':'0.22'}]. Labels first, then values."""
    n_labels = 0
    while n_labels < len(tokens) and not _NUMERIC.match(tokens[n_labels]):
        n_labels += 1
    if n_labels == 0 or n_labels == len(tokens) or (len(tokens) - n_labels) % n_labels:
        if len(tokens) % 2:
            return []
        n_labels = len(tokens) // 2                       # fallback: first half labels, second half one instance
    labels, values = tokens[:n_labels], tokens[n_labels:]
    rows: List[Dict[str, str]] = []
    for i in range(0, len(values), n_labels):
        inst = dict(zip(labels, values[i:i + n_labels]))
        inst["_raw"] = " ".join(f"{k}={v}" for k, v in inst.items())
        rows.append(inst)
    return rows


def parse_card(content: str) -> Optional[Alert]:
    """Alert from a MonitorFlow card's `content` JSON, or None if the card is not a MonitorFlow alert."""
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    title = _card_title(data)
    if not title:
        return None
    if title.lower() == _CHANNEL_TEST:
        return Alert(rule_name=title, raw_text=title)

    m = _CARD_TITLE.match(title)
    status = "firing"
    if not m:
        m = _CARD_RESOLVED.match(title)
        status = "resolved"
    if not m:
        return None

    kv: Dict[str, str] = {}
    instances: List[Dict[str, str]] = []
    pending: Optional[str] = None
    for row in data.get("elements") or []:
        tokens = _row_tokens(row)
        leftovers: List[str] = []
        for tok in tokens:
            if pending is not None:
                kv[pending] = tok
                pending = None
            elif _KEY_TOKEN.match(tok):
                pending = tok[:-1].strip()
            else:
                im = _INLINE_KV.match(tok)
                if im:
                    kv[im.group(1).strip()] = im.group(2).strip()
                else:
                    leftovers.append(tok)
        pending = None
        if len(leftovers) >= 2:
            instances.extend(_table(leftovers))

    low = {k.lower(): v for k, v in kv.items()}
    if "urgency" in low or "service" in low:
        return None                                       # that's one of our own PagerDuty cards (Zoho), never an alert

    alert = Alert(
        rule_name=m.group("rule"),
        status=status,
        severity=low.get("severity") or (m.group("sev") if status == "firing" else ""),
        firing_count=max(1, len(instances)),
        alert_time=low.get("time") or low.get("alert time", ""),
        alert_value=low.get("alert value") or "; ".join(i["_raw"] for i in instances)[:300],
        instances=instances,
        labels={k: v for k, v in kv.items() if k.lower() not in ("severity", "time", "alert time", "alert value")},
        raw_text=extract_text("interactive", content),
    )
    return alert


# --- plain-text template (simulate.sh, or if MonitorFlow ever posts text) -----------------

def parse(text: str) -> Optional[Alert]:
    """None if the text is not a MonitorFlow alert."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    m = _HEADER.match(lines[0])
    if not m:
        return None
    alert = Alert(
        rule_name=m.group("rule"),
        status=m.group("status").lower(),
        firing_count=int(m.group("count") or 1),
        raw_text=text,
    )
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith("severity:"):
            alert.severity = ln.split(":", 1)[1].strip()
        elif low.startswith("alert time:"):
            alert.alert_time = ln.split(":", 1)[1].strip()
        elif low.startswith("alert value:"):
            alert.alert_value = ln.split(":", 1)[1].strip()
        else:
            inst = {k: v.strip() for k, v in _KV.findall(ln)}
            inst["_raw"] = ln
            alert.instances.append(inst)
    return alert
