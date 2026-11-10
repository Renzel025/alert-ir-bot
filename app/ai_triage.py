"""AI triage: given an alert, the policy and past cases, decide `escalate` or `can_wait`.

This file is yours to finish — follow the # TODO steps in triage() (and classify_reply()
if you want natural-language replies understood beyond the keyword list).

Until triage() works, it raises NotImplementedError and pipeline.py pages every alert.
That is on purpose: the bot is safe from day one, and gets smarter when you finish this.

Reference: anthropic SDK — `pip install anthropic`, docs at https://platform.claude.com/docs
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config
from .alert_parser import Alert
from .policy import Policy

# What the pipeline expects back. Anything else = escalate (fail-safe).
VALID_DECISIONS = ("escalate", "can_wait")


@dataclass
class TriageResult:
    decision: str                 # "escalate" | "can_wait"
    reasoning: str                # one or two sentences; this is posted in the Lark thread
    confidence: str = "low"       # "high" | "medium" | "low"


SYSTEM_PROMPT = """You are the first-line triage for production alerts at an online casino platform.
You decide whether an alert needs on-call paged NOW ("escalate") or can wait a few minutes for a
human check person to look ("can_wait").

Rules:
- Player-facing money paths (payment, deposit, withdraw, login) are never "can_wait".
- Prefer "escalate" when unsure. A false page costs minutes; a missed incident costs hours.
- A drop that matches a past case that was marked normal by a human may be "can_wait".
- Keep reasoning to 1-2 sentences, written for the on-call engineer.

Reply with JSON only, no prose, exactly this shape:
{"decision": "escalate" | "can_wait", "reasoning": "<text>", "confidence": "high" | "medium" | "low"}"""


def _format_inputs(alert: Alert, policy: Policy, past_cases: List[Dict[str, Any]]) -> str:
    """Everything the model needs, as plain text. (Already done for you.)"""
    cases = "\n".join(
        f"- #{c['id']} severity={c.get('severity')} result={c.get('result')} detail={(c.get('detail') or '')[:120]}"
        for c in past_cases
    ) or "- none"
    instances = "\n".join(i.get("_raw", "") for i in alert.instances) or "- none"
    return (
        f"ALERT\nrule: {alert.rule_name}\nseverity: {alert.severity}\nfiring instances: {alert.firing_count}\n"
        f"alert time: {alert.alert_time}\nalert value: {alert.alert_value}\ninstances:\n{instances}\n\n"
        f"POLICY\nmatched rule: {policy.matched}\nwait minutes if can_wait: {policy.wait_minutes}\n\n"
        f"PAST CASES for this rule (newest first)\n{cases}"
    )


def triage(alert: Alert, policy: Policy, past_cases: List[Dict[str, Any]]) -> TriageResult:
    """Return a TriageResult. Raise on any failure — the pipeline turns exceptions into a page."""
    user_text = _format_inputs(alert, policy, past_cases)

    # TODO 1: create the client.
    #   import anthropic  ->  client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    #   (a missing key must raise, not return a default — the pipeline will page, which is what we want)

    # TODO 2: call the model.
    #   client.messages.create(model=config.AI_MODEL, max_tokens=..., system=SYSTEM_PROMPT,
    #                          messages=[{"role": "user", "content": user_text}],
    #                          output_config={"effort": "low"})
    #   Hints: this is a short classification -> a small max_tokens (~300) is fine.
    #          Opus 5.5 always thinks; "low" effort keeps it fast (we have a 5-min SLA).
    #          Give the client a timeout (anthropic.Anthropic(timeout=20.0)) — a hung call must not block the alert.

    # TODO 3: get the text out of the response.
    #   response.content is a list of blocks; take the ones where block.type == "text" and join block.text.
    #   Check response.stop_reason first: "refusal" or "max_tokens" -> raise.

    # TODO 4: parse and validate.
    #   data = json.loads(text)   (strip ```json fences first if the model adds them)
    #   data["decision"] must be in VALID_DECISIONS, otherwise raise ValueError.
    #   Return TriageResult(decision=..., reasoning=..., confidence=data.get("confidence", "low")).

    # TODO 5 (later): swap step 4 for structured outputs (output_config={"format": {...}}) so the JSON
    #   shape is guaranteed by the API instead of by json.loads + checks. See the docs for messages.parse().

    raise NotImplementedError("ai_triage.triage() not wired yet — see the TODO steps")


def classify_reply(text: str) -> Optional[str]:
    """Optional: turn a free-text reply from the check person into "normal" | "escalate" | None.
    Only called when the keyword list in pipeline.py did not match. Return None if unsure."""
    # TODO (optional): same pattern as triage(), smaller prompt, e.g.
    #   system: 'Classify an on-call reply as "normal" (alert is fine), "escalate" (needs on-call), or
    #            "unclear". Reply with one word.'
    #   Map the word to "normal" / "escalate" / None. Tagalog replies are common (ayos, hindi normal, tawag).
    raise NotImplementedError
