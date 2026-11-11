"""Post a fake alert card into the alert group, as the bot app itself (= the same sender MonitorFlow
uses), so the poller picks it up like a real alert: incident, thread, triage, check person / page.

    docker exec alert-ir-bot python -m scripts.post_test_alert            # on the server
    python -m scripts.post_test_alert                                      # on the Mac (venv)

Severity WARNING on purpose: it is below `severity_always_page`, so the alert goes through
ai_triage and, when that returns can_wait, tags the check person. Rule name "Bot-test".
Real Lark call; DRY_RUN=1 only logs."""
import json
import time

from app import alert_parser, config, lark_client

CARD = {
    "header": {"template": "orange", "title": {"tag": "plain_text", "content": "⚠️ WARNING - Bot-test"}},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"Severity: WARNING\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": "ENV: TEST\nDomain: PLATFORM\nPlatform: Aliyun"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "providerId\nratio\n3079\n0.22"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "Test alert posted by scripts/post_test_alert.py"}},
    ],
}

alert = alert_parser.parse_card(json.dumps(CARD, ensure_ascii=False))
assert alert and alert.rule_name == "Bot-test" and alert.severity == "WARNING", alert   # the bot must parse what we post
data = lark_client.post_card(config.ALERT_CHAT_ID, CARD)
print(f"posted {data.get('message_id')} to {config.ALERT_CHAT_ID}; the poller picks it up within {config.LARK_POLL_INTERVAL_SEC}s")
