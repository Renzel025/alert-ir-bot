"""All settings come from .env (see .env.example). Nothing secret lives in code."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Value from .env. An inline `# comment` after the value is dropped (seen 2026-11-09:
    `FIXED_GROUP_CHAT_ID=   # empty = …` made the comment the chat id and no alert paged)."""
    raw = (os.getenv(name) or "").strip()
    if raw.startswith("#"):
        raw = ""
    elif " #" in raw:
        raw = raw.split(" #", 1)[0].strip()
    return raw or default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --- Lark ------------------------------------------------------------------------
LARK_BASE = _env("LARK_OPEN_API_BASE", "https://open-sg.larksuite.com/open-apis").rstrip("/")
LARK_APP_ID = _env("LARK_APP_ID")
LARK_APP_SECRET = _env("LARK_APP_SECRET")
LARK_VERIFICATION_TOKEN = _env("LARK_VERIFICATION_TOKEN")  # from Event Subscriptions page
LARK_ENCRYPT_KEY = _env("LARK_ENCRYPT_KEY")                # optional; only if you enabled encryption

ALERT_CHAT_ID = _env("ALERT_CHAT_ID")                      # group where Provider_test_automation posts
ALERT_BOT_SENDER_ID = _env("ALERT_BOT_SENDER_ID")          # sender id of that bot (see README)
FIXED_GROUP_CHAT_ID = _env("FIXED_GROUP_CHAT_ID") or ALERT_CHAT_ID  # same group = reply under the alert
LARK_POLL_INTERVAL_SEC = _int("LARK_POLL_INTERVAL_SEC", 0)  # 0 = events only; >0 = also poll the group

# --- PagerDuty (we only send; Jeno owns the PagerDuty side) ---------------------------
PD_ROUTING_KEY = _env("PD_ROUTING_KEY")                    # Events API v2 integration key
PD_EVENTS_URL = _env("PD_EVENTS_URL", "https://events.pagerduty.com/v2/enqueue")
PD_STATUS_SECRET = _env("PD_STATUS_SECRET")                # Zoho Flow sends this in X-Status-Secret
ALERT_PUSH_SECRET = _env("ALERT_PUSH_SECRET")              # test-automation sends this in X-Alert-Secret to POST /alerts
POST_PD_STATUS_TO_THREAD = _env("POST_PD_STATUS_TO_THREAD", "0") == "1"  # 1 only if Zoho can't reply in thread
PD_API_KEY = _env("PD_API_KEY")                            # REST token; only for the Lark card buttons
PD_FROM_EMAIL = _env("PD_FROM_EMAIL")                      # PagerDuty needs a From header on writes

# --- AI --------------------------------------------------------------------------
# AI_BACKEND: claude_code = run the `claude` CLI headless on the Claude Code subscription (no API key;
#             on a server set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`),
#             anthropic = Anthropic SDK with ANTHROPIC_API_KEY, off = no AI calls at all.
AI_BACKEND = _env("AI_BACKEND", "claude_code")
AI_NOTES = _env("AI_NOTES", "1") == "1"                      # AI note on each PagerDuty card state
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
AI_MODEL = _env("AI_MODEL", "claude-opus-5-5")
AI_TIMEOUT_SEC = _int("AI_TIMEOUT_SEC", 90)                  # a hung model call must not pile up threads
CLAUDE_BIN = _env("CLAUDE_BIN", "claude")                    # path to the Claude Code CLI if not on PATH

# --- Behaviour -------------------------------------------------------------------
# Which alert cards open incidents: "threshold" (MonitorFlow / 🚨 CRITICAL cards), "test-automation" (red Web/PC run
# reports), or both. Renzel 2026-11-11: the server runs with test-automation only.
ALERT_SOURCES = {s.strip() for s in _env("ALERT_SOURCES", "threshold,test-automation").split(",") if s.strip()}
PAGERDUTY_ENABLED = _env("PAGERDUTY_ENABLED", "1") == "1"   # 0 = no PagerDuty at all: the bot's own card + a loud group alarm instead of a call
PD_ON_ACKNOWLEDGE = _env("PD_ON_ACKNOWLEDGE", "0") == "1"  # 1 = 'acknowledge' opens the PagerDuty incident already acknowledged;
                                                           # 0 (default) = PagerDuty only when a clock passes or Page on-call now is pressed
DEFAULT_WAIT_MINUTES = _int("DEFAULT_WAIT_MINUTES", 0)      # must-acknowledge deadline for the check person; 0 = none
DEFAULT_FIRING_MINUTES = _int("DEFAULT_FIRING_MINUTES", 20) # still open this long after the alert started → page
MEETING_HOURS = _int("MEETING_HOURS", 24)                   # "Create meeting" on the card: reservation lifetime
SWEEP_INTERVAL_SEC = _int("SWEEP_INTERVAL_SEC", 30)         # how often timeouts are checked
HTTP_TIMEOUT = _int("HTTP_TIMEOUT_SEC", 10)
DATABASE_PATH = _env("DATABASE_PATH", "alert_ir.sqlite3")
POLICY_PATH = _env("POLICY_PATH", "policy.yaml")
DRY_RUN = _env("DRY_RUN", "0") == "1"                       # 1 = log instead of calling Lark/PagerDuty
