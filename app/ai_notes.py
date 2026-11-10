"""AI notes on the PagerDuty card, one per state. Commentary only: this never decides whether to page
(that is ai_triage.py, still to be written). Any failure raises; the caller logs it and the card
simply has no note.

Backends (config.AI_BACKEND):
  claude_code  the Claude Code CLI run headless on the Claude Code subscription — no API key.
               `claude -p <prompt> --output-format json --tools "" --strict-mcp-config ...`
               On a server: `claude setup-token` once, put the token in .env as CLAUDE_CODE_OAUTH_TOKEN.
  anthropic    the Anthropic SDK with ANTHROPIC_API_KEY (when the team gets one).
  off          never called.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List

from . import config

log = logging.getLogger("alert-ir")

SYSTEM = """You write short notes on a Lark incident card for on-call engineers at an online casino platform
(providers, deposits, withdrawals, logins, game sessions). Plain text only: no markdown headers, no tables,
no emoji. Short lines, one idea each. Use only the facts in the input; if the input is thin, say what to check
rather than guessing. Never invent numbers, names or causes. Write for someone reading on a phone."""

STAGES: Dict[str, str] = {
    "triggered": (
        "STAGE: just triggered, on-call is being paged. Write at most 70 words: what fired (one line), "
        "the two most likely causes given these facts, and the first two or three checks to run."
    ),
    "escalated": (
        "STAGE: escalated to the next level because nobody acknowledged in time. Write at most 60 words as a "
        "handoff for the next responder: what is known, how long it has been firing, what to do first."
    ),
    "acknowledged": (
        "STAGE: a responder acknowledged. Write at most 50 words: the first three checks for this alert, "
        "in order, and what result would mean it is a false alarm."
    ),
    "resolved": (
        "STAGE: resolved. Write at most 70 words as the incident summary: how long it lasted, who acknowledged "
        "and resolved (from the timeline), the likely cause if the facts show one, and one lesson or follow-up."
    ),
}


def format_context(ctx: Dict[str, Any]) -> str:
    """Everything the model gets, as plain text. ctx keys: title, service, urgency, assignees,
    timeline (list of lines), alert (dict or None), past_cases (list)."""
    lines: List[str] = [
        f"PAGERDUTY INCIDENT\ntitle: {ctx.get('title')}\nservice: {ctx.get('service')}\n"
        f"urgency: {ctx.get('urgency')}\nassigned: {ctx.get('assignees') or '-'}",
        "TIMELINE (Lark card)\n" + ("\n".join(ctx.get("timeline") or []) or "-"),
    ]
    alert = ctx.get("alert") or {}
    if alert:
        inst = "\n".join(str(i.get("_raw", i)) for i in alert.get("instances") or []) or "-"
        labels = ", ".join(f"{k}={v}" for k, v in (alert.get("labels") or {}).items()) or "-"
        lines.append(
            f"ALERT (parsed by our bot)\nrule: {alert.get('rule_name')}\nseverity: {alert.get('severity')}\n"
            f"alert time: {alert.get('alert_time')}\nalert value: {alert.get('alert_value')}\n"
            f"labels: {labels}\ninstances:\n{inst}"
        )
    else:
        lines.append("ALERT: not parsed by our bot (manual or external PagerDuty incident); only the title is known.")
    cases = ctx.get("past_cases") or []
    if cases:
        lines.append("PAST CASES for this rule (newest first)\n" + "\n".join(
            f"- #{c.get('id')} severity={c.get('severity')} result={c.get('result')} detail={(c.get('detail') or '')[:120]}"
            for c in cases))
    return "\n\n".join(lines)


def note(stage: str, ctx: Dict[str, Any]) -> str:
    """One short note for this card state. Raises on any problem."""
    if stage not in STAGES:
        raise ValueError(f"no prompt for stage {stage!r}")
    if config.AI_BACKEND == "off":
        raise RuntimeError("AI_BACKEND=off")
    prompt = f"{STAGES[stage]}\n\n{format_context(ctx)}"
    if config.AI_BACKEND == "anthropic":
        text = _via_anthropic(prompt)
    elif config.AI_BACKEND == "claude_code":
        text = _via_claude_code(prompt)
    else:
        raise RuntimeError(f"unknown AI_BACKEND {config.AI_BACKEND!r}")
    text = text.strip()
    if not text:
        raise RuntimeError("empty model reply")
    return text[:1200]


def _via_claude_code(prompt: str) -> str:
    """Headless Claude Code. --tools "" and --strict-mcp-config (with no servers) keep it a plain
    model call; the cwd is a temp dir so it does not load this repo's CLAUDE.md or skills.
    Do NOT pass --bare: it also skips the keychain login and answers 'Not logged in'."""
    cmd = [
        config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "json", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--system-prompt", SYSTEM, "--model", config.AI_MODEL,
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_") or k == "CLAUDE_CODE_OAUTH_TOKEN"}
    env.pop("CLAUDECODE", None)  # never look like a nested session of the user's own Claude Code
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.AI_TIMEOUT_SEC,
                              cwd=tempfile.gettempdir(), env=env)
    except FileNotFoundError:
        raise RuntimeError(f"Claude Code CLI not found ({config.CLAUDE_BIN}); set CLAUDE_BIN or install it")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p timed out after {config.AI_TIMEOUT_SEC}s")
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"claude -p exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p returned non-JSON: {proc.stdout[:200]}")
    if data.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(data.get('result'))[:200]}")
    log.info("ai note: claude_code %s cost=$%.4f %sms", config.AI_MODEL, data.get("total_cost_usd") or 0, data.get("duration_ms"))
    return str(data.get("result") or "")


def _via_anthropic(prompt: str) -> str:
    import anthropic  # only needed for this backend

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=float(config.AI_TIMEOUT_SEC))
    response = client.messages.create(
        model=config.AI_MODEL, max_tokens=600, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low"},  # short note, 5-minute SLA; Opus 5.5 always thinks
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    return "".join(b.text for b in response.content if b.type == "text")
