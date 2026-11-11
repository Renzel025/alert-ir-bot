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
config.PD_ON_ACKNOWLEDGE = True        # most tests below exercise the acknowledge → PagerDuty path; the default (off) has its own test
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
pagerduty.trigger = lambda dedup, summary, sev, details: (PD.append({"dedup": dedup, "summary": summary, "op": "trigger"}) or {"status": "success"})
pagerduty.acknowledge = lambda dedup: (PD.append({"dedup": dedup, "op": "acknowledge"}) or {"status": "success"})
pagerduty.resolve = lambda dedup: (PD.append({"dedup": dedup, "op": "resolve"}) or {"status": "success"})
PD_DOWN = {"on": False}
_real_trigger = pagerduty.trigger


def _trigger_maybe_down(dedup, summary, sev, details):
    if PD_DOWN["on"]:
        raise pagerduty.PagerDutyError("trigger: HTTP 503")
    return _real_trigger(dedup, summary, sev, details)


pagerduty.trigger = _trigger_maybe_down
ai_triage.triage = lambda alert, pol, cases: ai_triage.TriageResult(decision="can_wait", reasoning="stub", confidence="low")
policy_mod.lookup = lambda rule, sev: policy_mod.Policy(**{**POLICY.to_dict()})


def _wait():
    for _ in range(2):
        for t in threading.enumerate():
            if t.name.startswith(("act-", "paint-", "ai-")):
                t.join(2)


AI_NOTES_MADE: List[str] = []


def fake_note(stage, ctx):
    AI_NOTES_MADE.append(stage)
    return f"summary: {ctx['title']} lasted a few minutes; {ctx['timeline'][-1]}"


from app import ai_notes  # noqa: E402
ai_notes.note = fake_note


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


# --- scenario A: normal threshold, acknowledged while PagerDuty is DOWN (bot card fallback), resolved as normal

def test_prompt_goes_to_the_group_with_the_firing_clock():
    PD_DOWN["on"] = True
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


def test_acknowledge_with_pagerduty_down_posts_the_bot_card():
    config.AI_NOTES = True
    iid = db.find_open_incident("bot-test")["id"]
    assert reply(iid, MATE, "ack, checking now") == "acknowledged"
    _wait()
    inc = db.get_incident(iid)
    assert inc["status"] == "checking" and inc["acked_by"] == MATE and not inc["pd_opened_at"]
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 20 * 60)) < 1      # firing clock unchanged
    assert "PagerDuty is unreachable" in _last("post_text")["text"]
    card = _last("post_card")["card"]
    assert card["header"]["template"] == "yellow" and card["header"]["title"]["content"] == "Bot-test"
    assert _buttons(card) == ["Resolve", "Create meeting", "Page on-call now"]
    assert "I page on-call at" in _facts(card)
    assert f"🔎 Acknowledged by <at id={MATE}></at>" in card["elements"][2]["text"]["content"]
    assert inc["card_message_id"].startswith("om_")
    # the AI note for the acknowledged stage is painted onto the same card a moment later
    assert AI_NOTES_MADE == ["acknowledged"]
    painted = _last("update_card")["card"]
    assert "🤖 **AI note (acknowledged)**\nsummary: [WARNING] Bot-test" in painted["elements"][4]["text"]["content"]
    assert _buttons(painted) == ["Resolve", "Create meeting", "Page on-call now"]


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
    _wait()
    inc = db.get_incident(iid)
    assert inc["status"] == "resolved" and PD == []                            # never reached PagerDuty
    assert any("🟢 Bot-test:" in c["text"] and "no page" in c["text"] for c in LARK if c["op"] == "post_text")
    # no PagerDuty card → the resolved AI note lands on the bot's card and is stored as the ai_summary outcome
    assert AI_NOTES_MADE == ["acknowledged", "resolved"]
    painted = _last("update_card")["card"]
    assert painted["header"]["template"] == "green"
    assert "🤖 **AI note (resolved)**" in painted["elements"][4]["text"]["content"]
    assert [d["decision"] for d in db.decisions(iid) if d["stage"] == "ai_note"] == ["acknowledged", "resolved"]
    assert _buttons(painted) == [] and "Join the incident call" not in _facts(painted)
    assert DELETED == ["rsv1"]
    config.AI_NOTES = False
    PD_DOWN["on"] = False


def test_normal_before_any_ack_posts_the_ai_summary_in_the_group():
    config.AI_NOTES = True
    AI_NOTES_MADE.clear()
    iid = alert("Quick-false-alarm")
    assert reply(iid, CP, "false alarm, all good") == "resolved_normal"
    _wait()
    assert AI_NOTES_MADE == ["resolved"]
    msg = _last("post_text")["text"]
    assert msg.startswith("🤖 AI note (resolved) for Quick-false-alarm")
    config.AI_NOTES = False


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
    assert inc["status"] == "paged" and [p["op"] for p in PD] == ["trigger"] and PD[0]["dedup"] == inc["pd_dedup_key"]
    assert "No acknowledgement within 5 min" in _last("post_text")["text"]


# --- scenario C: acknowledged → opened in PagerDuty as acknowledged; still firing after the window → re-trigger

def test_acknowledge_opens_pagerduty_already_acknowledged():
    PD.clear()
    POLICY.wait_minutes = 5
    iid = alert("Provider-drop", "P1")
    n_cards = len([c for c in LARK if c["op"] == "post_card"])
    assert reply(iid, CP, "on it") == "acknowledged"
    inc = db.get_incident(iid)
    assert inc["status"] == "checking" and inc["pd_opened_at"]
    assert [p["op"] for p in PD] == ["trigger", "acknowledge"] and PD[0]["dedup"] == inc["pd_dedup_key"]
    assert abs(inc["wait_deadline"] - (inc["created_at"] + 20 * 60)) < 1      # ack swaps the 5 min clock for the firing clock
    assert "Opened in PagerDuty as acknowledged (no call)" in _last("post_text")["text"]
    assert len([c for c in LARK if c["op"] == "post_card"]) == n_cards       # no bot card: the PagerDuty card is the card


def test_normal_after_ack_resolves_pagerduty_too():
    iid = db.find_open_incident("provider-drop")["id"]
    assert reply(iid, CP, "all good, normal") == "resolved_normal"
    assert [p["op"] for p in PD] == ["trigger", "acknowledge", "resolve"]


def test_ack_then_firing_window_passes_retriggers_pagerduty():
    PD.clear()
    iid = alert("Provider-drop-2", "P1")
    assert reply(iid, CP, "ack") == "acknowledged"
    db.update_incident(iid, wait_deadline=time.time() - 1)
    assert pipeline.sweep_timeouts() == 1
    assert db.get_incident(iid)["status"] == "paged"
    assert [p["op"] for p in PD] == ["trigger", "acknowledge", "trigger"]    # same dedup key: PagerDuty re-opens and calls
    assert "Still firing" in _last("post_text")["text"] and "acknowledged, not resolved" in _last("post_text")["text"]


# --- scenario D: Page on-call now button (bot card fallback) ------------------------------

def test_page_now_button_pages_for_anyone():
    PD.clear()
    POLICY.wait_minutes = 0
    iid = alert("Cashier-errors")
    PD_DOWN["on"] = True
    reply(iid, MATE, "checking")                                              # PagerDuty down → bot card
    PD_DOWN["on"] = False
    assert click(iid, "bot_page", MATE)["toast"]["content"] == "Paging on-call…"
    assert db.get_incident(iid)["status"] == "paged" and [p["op"] for p in PD] == ["trigger"]
    card = _last("update_card")["card"]
    assert card["header"]["template"] == "red" and _buttons(card) == []
    assert "📟 Paged on-call" in card["elements"][2]["text"]["content"]
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


def test_without_pagerduty_the_bot_card_and_a_group_alarm_replace_the_call():
    config.PAGERDUTY_ENABLED = False
    PD.clear()
    iid = alert("No-pd-rule")
    assert reply(iid, CP, "acknowledge") == "acknowledged"
    _wait()
    assert PD == [] and db.get_incident(iid)["card_message_id"]                  # bot card, PagerDuty never touched
    assert "I raise the alarm here" in [c for c in LARK if c["op"] == "post_text"][-1]["text"]
    db.update_incident(iid, wait_deadline=time.time() - 1)
    assert pipeline.sweep_timeouts() == 1
    assert PD == [] and db.get_incident(iid)["status"] == "paged"
    msg = _last("post_text")["text"]
    assert msg.startswith("🚨 No-pd-rule:") and f'<at user_id="{CP}">' in msg and "this message is the alarm" in msg
    card = _last("update_card")["card"]
    assert card["header"]["template"] == "red" and "🚨 Alarm raised" in card["elements"][2]["text"]["content"]
    config.PAGERDUTY_ENABLED = True


def test_default_acknowledge_stays_in_lark_and_pagerduty_only_on_timeout():
    config.PD_ON_ACKNOWLEDGE = False
    PD.clear()
    iid = alert("Lark-only-ack")
    assert reply(iid, MATE, "ack") == "acknowledged"
    _wait()
    assert PD == [] and db.get_incident(iid)["card_message_id"]                  # bot card, nothing in PagerDuty yet
    assert "I page on-call via PagerDuty" in [c for c in LARK if c["op"] == "post_text"][-1]["text"]
    assert _buttons(_last("post_card")["card"]) == ["Resolve", "Create meeting", "Page on-call now"]
    db.update_incident(iid, wait_deadline=time.time() - 1)
    assert pipeline.sweep_timeouts() == 1
    assert [p["op"] for p in PD] == ["trigger"] and db.get_incident(iid)["status"] == "paged"   # PagerDuty only now
    config.PD_ON_ACKNOWLEDGE = True


def test_alert_sources_filter_ignores_threshold_cards():
    config.ALERT_SOURCES = {"test-automation"}
    text = "[FIRING:1] Threshold-only\nSeverity: WARNING\nalert time: 2026-11-11 12:00:00\nalert value: x"   # source = threshold
    out = pipeline.handle_message({"message_id": "om_alert_thr", "chat_id": "oc_test", "sender_id": "cli_alertbot",
                                   "message_type": "text", "content": json.dumps({"text": text})})
    assert out == "ignored_source" and db.find_open_incident("threshold-only") is None
    config.ALERT_SOURCES = {"threshold", "test-automation"}


def test_pushed_test_run_opens_and_a_green_push_resolves():
    body = {"source": "test-automation", "provider": "CQ9 3079", "game": "5 God beasts", "duration": "23s",
            "steps": [{"step": "Enter Casino Plus", "ok": True}, {"step": "Enter provider game", "ok": True},
                      {"step": "Place bet", "ok": False, "note": "PLAY never clicked"}],
            "summary": "no bet was placed", "lark_message_id": "om_run_card_1"}
    POLICY.wait_minutes = 5
    out = pipeline.handle_alert_push(body)
    assert out["ok"] and out["result"] == "waiting" and out["rule"] == "test-automation: CQ9 3079"
    inc = db.get_incident(out["incident_id"])
    assert inc["severity"] == "HIGH" and inc["thread_root_message_id"] == "om_run_card_1"   # thread under the run card
    assert json.loads(inc["alert_json"])["alert_value"] == "Place bet ❌ PLAY never clicked"
    assert not any(c["op"] == "reply" and "Incident #" in c["text"] for c in LARK)        # no 👀 opener any more
    assert pipeline.handle_alert_push(body)["result"] == "duplicate"                      # same card id twice
    green = dict(body, steps=[dict(s, ok=True) for s in body["steps"]], lark_message_id="om_run_card_2")
    assert pipeline.handle_alert_push(green)["result"] == "resolved"
    assert db.get_incident(out["incident_id"])["status"] == "resolved"
    # no card id (the server-side curl test): the opener is a fresh root message, never a reply to a fake id
    n_replies = len([c for c in LARK if c["op"] == "reply"])
    out2 = pipeline.handle_alert_push(dict(body, provider="JILI", lark_message_id=""))
    inc2 = db.get_incident(out2["incident_id"])
    assert inc2["thread_root_message_id"] == inc2["prompt_message_id"]                   # the 🟡 prompt is the root
    assert len([c for c in LARK if c["op"] == "reply"]) == n_replies
    assert [d for d in db.decisions(out2["incident_id"]) if d["stage"] == "lark"] == []    # no Lark error logged
    POLICY.wait_minutes = 0


def test_ai_timeline_has_names_not_ids_and_no_internal_errors():
    config.AI_NOTES = True
    config.PD_ON_ACKNOWLEDGE = False            # bot-card path, so the incident's own notes are written
    AI_NOTES_MADE.clear()
    CTX: List[Dict[str, Any]] = []
    orig = ai_notes.note
    ai_notes.note = lambda stage, ctx: (CTX.append(ctx) or orig(stage, ctx))
    iid = alert("Names-check")
    db.log_decision(iid, "lark", "error", "LarkError: reply_text: HTTP 400 code=99992354")   # internal plumbing
    reply(iid, MATE, "ack")
    _wait()
    reply(iid, CP, "normal")
    _wait()
    ai_notes.note = orig
    text = "\n".join(CTX[-1]["timeline"]) + " " + str(CTX[-1]["assignees"])
    assert "ou_" not in text and "99992354" not in text
    assert "Renzel" in text and "a teammate" in text
    from app.ai_notes import format_context
    assert "ou_" not in format_context(CTX[-1]) and "<at" not in format_context({"timeline": ["🟡 Acknowledged by <at id=ou_abc></at> 10:00"]})
    config.AI_NOTES = False
    config.PD_ON_ACKNOWLEDGE = True


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
