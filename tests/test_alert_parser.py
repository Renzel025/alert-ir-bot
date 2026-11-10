"""Parser checks against real cards copied from the Lark alert group (2026-11-09).

Run from alert-ir-bot/:   python -m tests.test_alert_parser      (no pytest needed)
                   or:    python -m pytest tests/

Hard-coded on purpose: these are fixed inputs with known expected answers. When MonitorFlow's
real alert card is seen, add it here first, then make parse_card() pass.
"""
from __future__ import annotations

import json

from app import alert_parser as ap


def T(s: str) -> dict:
    return {"tag": "text", "text": s}


def card(title: str, *rows) -> str:
    return json.dumps({"title": title, "elements": list(rows)}, ensure_ascii=False)


# --- real cards seen in the group --------------------------------------------------------

THRESHOLD_ALERT = card(                      # Renzel's threshold alerting, same app, same group
    "🚨 CRITICAL - Renzel-test",
    [T("🔴 "), T("Severity:"), T(" CRITICAL"), T("🕐 "), T("Time:"), T("2026-11-09 11:34:06")],
    [T("ENV: "), T("PROD"), T("Domain: "), T("PLATFORM"), T("Platform: "), T("Aliyun")],
    [{"tag": "hr"}],
    [T("providerId"), T("ratio"), T("3079"), T("0.22829700775766532")],
    [{"tag": "hr"}],
    [T("🙋 Report wrong alert click👆")],
)
ANOMALY_FOLLOWUP = card(                     # lark-alert-listener reacting to the alert
    "⚠️ Anomaly detected - IGO Distributions of Providers GENERAL",
    [T("Received abnormalities and anomalies in Grafana graph "), T("IGO Distributions of Providers GENERAL")],
    [T("Provider:"), T(" CQ9 (3079)　"), T("Ratio:"), T(" 0.23 (drop)")],
)
WEBPC_FOLLOWUP = card(
    "Web/PC automatic testing for provider: CQ9 3079 (5 God beasts)",
    [T("Duration: 1m 25s\nEnter Casino Plus ✅")],
)
CHANNEL_TEST = card("MonitorFlow channel test", [T("SNSoft Monitoring System Test")])
ZOHO_PD_CARD = card(                         # our own PagerDuty status card, posted by Zoho
    "card button test 10",
    [T("Urgency:"), T(" high · "), T("Service:"), T(" alert-ir-bot-test\\n")],
)

# --- synthetic variations ----------------------------------------------------------------

TWO_INSTANCES = card(
    "⚠️ WARNING - provider drop",
    [T("Severity:"), T(" WARNING"), T("Time:"), T("2026-11-09 12:00:00")],
    [T("providerId"), T("ratio"), T("3079"), T("0.22"), T("3080"), T("0.51")],
)
RESOLVED_GUESS = card("✅ RESOLVED - Renzel-test", [T("Time:"), T("2026-11-09 12:10:00")])  # recovery card not seen yet
ZOHO_WITH_ALERT_LIKE_TITLE = card("CRITICAL - Renzel-test", [T("Urgency:"), T(" high · "), T("Service:"), T(" x")])


def test_threshold_alert_is_parsed():
    a = ap.parse_card(THRESHOLD_ALERT)
    assert a is not None
    assert a.rule_name == "Renzel-test" and a.status == "firing" and a.severity == "CRITICAL"
    assert a.alert_time == "2026-11-09 11:34:06"
    assert a.labels == {"ENV": "PROD", "Domain": "PLATFORM", "Platform": "Aliyun"}
    assert a.instances == [{"providerId": "3079", "ratio": "0.22829700775766532", "_raw": "providerId=3079 ratio=0.22829700775766532"}]
    assert a.firing_count == 1 and a.fingerprint == "renzel-test" and not a.is_test


def test_two_instances_table():
    a = ap.parse_card(TWO_INSTANCES)
    assert a is not None and a.firing_count == 2 and a.severity == "WARNING"
    assert [i["providerId"] for i in a.instances] == ["3079", "3080"]


def test_resolved_title_guess():
    a = ap.parse_card(RESOLVED_GUESS)
    assert a is not None and a.status == "resolved" and a.fingerprint == "renzel-test"


def test_channel_test_is_a_test():
    a = ap.parse_card(CHANNEL_TEST)
    assert a is not None and a.is_test


def test_other_cards_are_rejected():
    for c in (ANOMALY_FOLLOWUP, WEBPC_FOLLOWUP, ZOHO_PD_CARD, ZOHO_WITH_ALERT_LIKE_TITLE):
        assert ap.parse_card(c) is None, c[:60]


def test_text_template_still_works():            # scripts/simulate.sh path
    a = ap.parse("[FIRING:2] provider_drop_rate\nSeverity: P1\nalert time: 10:00\nalert value: < 0.5\nproviderId=1 ratio=0.2\nproviderId=2 ratio=0.3")
    assert a is not None and a.firing_count == 2 and a.severity == "P1" and len(a.instances) == 2


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                failed += 1
                print("FAIL", name, e)
    sys.exit(1 if failed else 0)
