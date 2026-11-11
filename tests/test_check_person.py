"""The check-person path as of 2026-11-11: 🟡 prompt in the group → 'normal' resolves, 'acknowledge'
posts the bot's own incident card (Resolve / Create meeting / Page on-call now), two clocks page.
Run: python -m tests.test_check_person   (no pytest, no network, temp database; definition order matters)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List

from app import ai_triage, config, db, lark_client, pagerduty, pipeline
import app.policy as policy_mod

config.DATABASE_PATH = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
config.DRY_RUN = False
config.ALERT_CHAT_ID = "oc_test"
config.FIXED_GROUP_CHAT_ID = "oc_test"
config.ALERT_BOT_SENDER_ID = "cli_alertbot"
config.AI_NOTES = False
db.init()

CP = "ou_checkperson"
MATE = "ou_teammate"
LARK: List[Dict[str, Any]] = []
PD: List[Dict[str, Any]] = []
MEETINGS: List[str] = []
DELETED: List[str] = []
_ids = iter(range(100, 999))
POLICY = policy_mod.Policy(always_page=False, check_person=CP, check_person_name="Renzel", wait_minutes=0, firing_minutes=20)


def _rec(op, **kw):
    LARK.append({"op": op, **kw})
    return {"message_id": f"om_{next(_ids)}"}


lark_client.reply_text = lambda mid, text, in_thread=True: _rec("reply", to=mid, text=text, in_thread=in_thread)
lark_client.post_text = lambda chat, text: _rec("post_text", chat=chat, text=text)
lark_client.post_card = lambda chat, card: _rec("post_card", chat=chat, card=card)
lark_client.update_card = lambda mid, card: _rec("update_card", to=mid, card=card)
lark_client.post_rich = lambda chat, title, lines: _rec("post_rich", chat=chat, title=title, lines=lines)
lark_client.create_meeting = lambda topic, owner, hours=24: (MEETINGS.append(owner) or {"id": f"rsv{len(MEETINGS)}", "url": f"https://vc/{len(MEETINGS)}"})
lark_client.delete_meeting = lambda rid: DELETED.append(rid)
pagerduty.trigger = lambda dedup, summary, sev, details: (PD.append({"dedup": dedup, "summary": summary}) or {"status": "success"})
ai_triage.triage = lambda alert, pol, cases: ai_triage.TriageResult(decision="can_wait", reasoning="stub", confidence="low")
policy_mod.lookup = lambda rule, sev: policy_mod.Policy(**{**POLICY.to_dict()})


def _wait():
    for _ in range(2):
        for t in threading.enumerate():
            if t.name.startswith(("act-", "paint-")):
                t.join(2)


def alert(rule: str, severity: str = "WARNING") -> int:
    text = f"[FIRING:1] {rule}\nSeverity: {severity}\nalert time: 2026-11-11 12:00:00\nalert value: x"
    out = pipeline.handle_message({"message_id": f"om_alert_{rule}", "chat_id": "oc_test", "sender_id": "cli_alertbot",
                                   "message_type": "text", "content": json.dumps({"text": text})})
    assert out == "waiting", out
    return int(db.find_open_incident(rule.lower())["id"])


def reply(iid: int, who: str, text: str) -> str:
    inc = db.get_incident(iid)
    return pipeline.handle_message({"message_id": f"om_r_{next(_ids)}", "chat_id": "oc_test", "sender_id": who,
                                    "sender_type": "user", "message_type": "text", "parent_id": inc["prompt_message_id"],
                                    "root_id": inc["prompt_message_id"], "content": json.dumps({"text": text})})


def click(iid: int, action: str, who: str) -> Dict[str, Any]:
    resp = pipeline.handle_card_action({"action": {"value": {"action": action, "incident_id": str(iid), "kind": "incident"}},
                                        "operator": {"open_id": who}, "context": {"open_message_id": "om_card"}})
    _wait()
    return resp


def _last(op):
    return [c for c in LARK if c["op"] == op][-1]


def _buttons(card):
    rows = [e for e in card["elements"] if e["tag"] == "action"]
    return [a["text"]["content"] for a in rows[0]["actions"]] if rows else []


def _facts(card):
    return card["elements"][0]["text"]["content"]


# --- scenario A: normal threshold, acknowledged, resolved as normal ------------------------

def test_prompt_goes_to_the_group_with_the_firing_clock():
    iid = alert("Bot-test")
    inc = db.get_incident(iid)
    msg = _last("post_text")
    assert msg["chat"] == "oc_test" and "🟡 Bot-test (WARNING)" in msg["text"] and "'acknowledge'" in msg["text"]
    assert "still firing 20 min after it started" in msg["text"]
    assert inc["prompt_message_id"].startswith("om_")
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 20 * 60)) < 1      # firing clock, no ack deadline


def test_not_normal_is_unclear_not_escalate():
    iid = db.find_open_incident("bot-test")["id"]
    assert reply(iid, CP, "this is not normal") == "reply_unclear"
    assert "I couldn't tell" in _last("post_text")["text"]
    assert db.get_incident(iid)["status"] == "waiting"


def test_acknowledge_by_anyone_posts_the_yellow_card():
    iid = db.find_open_incident("bot-test")["id"]
    assert reply(iid, MATE, "ack, checking now") == "acknowledged"
    inc = db.get_incident(iid)
    assert inc["status"] == "checking" and inc["acked_by"] == MATE
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 20 * 60)) < 1      # firing clock unchanged
    card = _last("post_card")["card"]
    assert card["header"]["template"] == "yellow" and card["header"]["title"]["content"] == "Bot-test"
    assert _buttons(card) == ["Resolve", "Create meeting", "Page on-call now"]
    assert "I page on-call at" in _facts(card)
    assert f"🔎 Acknowledged by <at id={MATE}></at>" in card["elements"][2]["text"]["content"]
    assert inc["card_message_id"].startswith("om_")


def test_second_ack_changes_nothing():
    iid = db.find_open_incident("bot-test")["id"]
    n = len(LARK)
    assert reply(iid, CP, "acknowledged") == "reply_context"
    assert len(LARK) == n


def test_create_meeting_on_the_incident_card():
    iid = db.find_open_incident("bot-test")["id"]
    assert click(iid, "meeting", CP)["toast"]["type"] == "info"
    assert MEETINGS == [CP]
    card = _last("update_card")["card"]
    assert "[Join the incident call](https://vc/1)" in _facts(card)
    assert _buttons(card) == ["Resolve", "Page on-call now"]                 # button gone once a link exists
    assert _last("post_text")["text"].endswith("join in meeting link\nhttps://vc/1")
    click(iid, "meeting", MATE)
    assert MEETINGS == [CP]                                                  # one meeting per incident


def test_resolve_button_is_check_person_only():
    iid = db.find_open_incident("bot-test")["id"]
    assert click(iid, "bot_resolve", MATE)["toast"]["type"] == "error"
    assert db.get_incident(iid)["status"] == "checking"
    assert reply(iid, MATE, "its normal") == "reply_not_authorized"
    assert db.get_incident(iid)["status"] == "checking"


def test_normal_from_check_person_resolves_and_cancels_the_meeting():
    iid = db.find_open_incident("bot-test")["id"]
    assert reply(iid, CP, "normal") == "resolved_normal"
    inc = db.get_incident(iid)
    assert inc["status"] == "resolved" and PD == []
    assert "🟢 Bot-test:" in _last("post_text")["text"] and "no page" in _last("post_text")["text"]
    card = _last("update_card")["card"]
    assert card["header"]["template"] == "green" and _buttons(card) == [] and "Join the incident call" not in _facts(card)
    assert DELETED == ["rsv1"]


# --- scenario B: high threshold, nobody acknowledges within 5 min ------------------------

def test_high_threshold_has_the_5_min_ack_clock():
    POLICY.wait_minutes = 5
    iid = alert("Login-latency", "P1")
    inc = db.get_incident(iid)
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 5 * 60)) < 1
    assert "No acknowledgement within 5 min" in _last("post_text")["text"]


def test_no_ack_in_time_pages():
    iid = db.find_open_incident("login-latency")["id"]
    db.update_incident(iid, wait_deadline=time.time() - 1)                   # pretend 5 min passed
    assert pipeline.sweep_timeouts() == 1
    inc = db.get_incident(iid)
    assert inc["status"] == "paged" and len(PD) == 1 and PD[0]["dedup"] == inc["pd_dedup_key"]
    assert "No acknowledgement within 5 min" in _last("post_text")["text"]


# --- scenario C: acknowledged, but still firing after the window ---------------------------

def test_ack_then_firing_window_passes_pages_and_paints_the_card_red():
    POLICY.wait_minutes = 5
    iid = alert("Provider-drop", "P1")
    assert reply(iid, CP, "on it") == "acknowledged"
    inc = db.get_incident(iid)
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 20 * 60)) < 1      # ack swaps the 5 min clock for the firing clock
    db.update_incident(iid, wait_deadline=time.time() - 1)
    assert pipeline.sweep_timeouts() == 1
    assert db.get_incident(iid)["status"] == "paged" and len(PD) == 2
    assert "Still firing" in _last("post_text")["text"] and "acknowledged, not resolved" in _last("post_text")["text"]
    card = _last("update_card")["card"]
    assert card["header"]["template"] == "red" and _buttons(card) == []
    assert "📟 Paged on-call" in card["elements"][2]["text"]["content"]


# --- scenario D: Page on-call now button --------------------------------------------------

def test_page_now_button_pages_for_anyone():
    POLICY.wait_minutes = 0
    iid = alert("Cashier-errors")
    reply(iid, MATE, "checking")
    assert click(iid, "bot_page", MATE)["toast"]["content"] == "Paging on-call…"
    assert db.get_incident(iid)["status"] == "paged" and len(PD) == 3
    assert click(iid, "bot_page", MATE)["toast"]["content"] == "This incident is already closed or paged."


def test_ignore_on_the_pagerduty_card_mutes_repeats_of_the_alert():
    iid = db.find_open_incident("cashier-errors")["id"]                      # paged above
    db.upsert_pd_card("Q9", incident_id=iid, state="triggered", lark_message_id="om_pd", chat_id="oc_test")
    pagerduty.snooze = lambda incident_id, minutes: {"id": incident_id, "status": "acknowledged", "title": "x"}
    pipeline._do_card_action("snooze", "Q9", MATE, 30, "om_pd")
    inc = db.get_incident(iid)
    assert abs(inc["muted_until"] - (time.time() + 30 * 60)) < 2
    n = len(LARK)
    text = "[FIRING:2] Cashier-errors\nSeverity: WARNING\nalert time: 2026-11-11 12:10:00\nalert value: x"
    out = pipeline.handle_message({"message_id": "om_alert_repeat", "chat_id": "oc_test", "sender_id": "cli_alertbot",
                                   "message_type": "text", "content": json.dumps({"text": text})})
    assert out == "muted" and len([c for c in LARK[n:] if c["op"] == "reply"]) == 0   # no "Still firing"
    db.update_incident(iid, muted_until=time.time() - 1)                      # window over
    out = pipeline.handle_message({"message_id": "om_alert_repeat2", "chat_id": "oc_test", "sender_id": "cli_alertbot",
                                   "message_type": "text", "content": json.dumps({"text": text})})
    assert out == "repeat" and "Still firing" in _last("reply")["text"]


def test_reply_words():
    c = pipeline.classify_reply
    assert c("normal") == "normal" and c("this is normal") == "normal" and c("ok fine") == "normal"
    assert c("ack") == "ack" and c("acknowledge") == "ack" and c("on it") == "ack" and c("i will check") == "ack"
    assert c("not normal") is None and c("not ok") is None and c("abnormal") is None
    assert c("escalate") is None and c("hmm") is None


if __name__ == "__main__":
    import sys
    names = [n for n, v in list(globals().items()) if n.startswith("test_") and callable(v)]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print(f"PASS {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {n}: {e}")
    print(f"\n{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
