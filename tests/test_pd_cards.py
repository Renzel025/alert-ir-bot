"""One card per PagerDuty incident: Zoho events and button clicks repaint the same Lark message.
Run: python -m tests.test_pd_cards   (no pytest, no network, temp database)."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List

from app import ai_notes, cards, config, db, lark_client, pagerduty, pipeline

config.DATABASE_PATH = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
config.DRY_RUN = False
config.FIXED_GROUP_CHAT_ID = "oc_test"
config.AI_NOTES = True
config.AI_BACKEND = "claude_code"
db.init()

AI_CALLS: List[str] = []


def fake_note(stage, ctx):
    AI_CALLS.append(stage)
    if stage == "escalated":
        raise RuntimeError("model down")        # the card must survive an AI failure
    return f"note for {stage}: {ctx['title']}"


ai_notes.note = fake_note

LARK_CALLS: List[Dict[str, Any]] = []
_ids = iter(range(100, 200))


def fake_post_card(chat_id, card):
    LARK_CALLS.append({"op": "post", "chat": chat_id, "card": card})
    return {"message_id": f"om_{next(_ids)}"}


def fake_update_card(message_id, card):
    LARK_CALLS.append({"op": "update", "id": message_id, "card": card})
    return {}


PD_INC = {"id": "Q1", "title": "card test", "html_url": "https://pd/Q1", "urgency": "high",
          "service": {"summary": "Bot Service"}, "assignments": [{"assignee": {"summary": "Renzel Hernandez"}}]}

MEETING_CALLS: List[Dict[str, Any]] = []


def fake_create_meeting(topic, owner_open_id, hours=24):
    MEETING_CALLS.append({"topic": topic, "owner": owner_open_id, "hours": hours})
    return {"id": f"rsv{len(MEETING_CALLS)}", "url": f"https://vc.larksuite.com/j/{len(MEETING_CALLS)}", "meeting_no": "123456789"}


MEETING_DELETED: List[str] = []
lark_client.delete_meeting = lambda reserve_id: MEETING_DELETED.append(reserve_id)


def fake_post_text(chat_id, text):
    LARK_CALLS.append({"op": "post_text", "chat": chat_id, "text": text})
    return {"message_id": f"om_{next(_ids)}"}


lark_client.post_card = fake_post_card
lark_client.update_card = fake_update_card
lark_client.create_meeting = fake_create_meeting
lark_client.post_text = fake_post_text
pagerduty.set_status = lambda incident_id, status: dict(PD_INC, status=status)
pagerduty.snooze = lambda incident_id, minutes: dict(PD_INC, status="acknowledged")
pagerduty.get_incident = lambda incident_id: dict(PD_INC, id=incident_id)   # every test id "exists"


def _buttons(card):
    acts = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"]
    return [a["text"]["content"] if a["tag"] == "button" else "Ignore for..." for a in acts]


def _history(card):
    # element order: facts div, [hr, history div], [hr, AI note div], action row
    divs = [e["text"]["content"] for e in card["elements"] if e["tag"] == "div"]
    return divs[1] if len(divs) > 1 and not divs[1].startswith("🤖") else ""


def _last(op):
    return [c for c in LARK_CALLS if c["op"] == op][-1]


def zoho(event, **extra):
    body = {"event": f"incident.{event}", "incident_id": "Q1", "title": "card test", "html_url": "https://pd/Q1",
            "urgency": "high", "service": "Bot Service", "incident_key": "k1", "agent": "Renzel Hernandez"}
    body.update(extra)
    out = pipeline.handle_pd_status(body)
    _wait_paint()
    return out


def _wait_paint():
    """Clicks push to Lark, and new states fetch an AI note, from background threads; wait for them."""
    import threading
    for _ in range(2):   # an act- thread spawns paint-/ai- threads of its own
        for t in threading.enumerate():
            if t.name.startswith(("act-", "paint-", "ai-")):
                t.join(2)


def _ai_note(card):
    divs = [e["text"]["content"] for e in card["elements"] if e["tag"] == "div"]
    return next((d for d in divs if d.startswith("🤖")), "")


def click(action, option=None, who="ou_me", msg="om_100"):
    act = {"value": {"action": action, "incident_id": "Q1"}}
    if option:
        act["option"] = option
    resp = pipeline.handle_card_action({"action": act, "operator": {"open_id": who}, "context": {"open_message_id": msg}})
    _wait_paint()
    return resp


def test_trigger_posts_red_card_with_all_buttons():
    out = zoho("triggered")
    assert out["card"] == "painted", out
    assert _last("post")["chat"] == "oc_test"
    card = _last("post")["card"]
    assert card["header"]["template"] == "red"
    assert _buttons(card) == ["Acknowledge", "Resolve / Normal", "📞 Call on duty", "Open in PagerDuty"]
    assert card["config"]["update_multi"] is True
    assert db.get_pd_card("Q1")["lark_message_id"] == "om_100"


def test_ai_note_is_painted_after_trigger():
    assert AI_CALLS == ["triggered"]
    card = LARK_CALLS[-1]["card"]                       # the repaint done by the AI thread
    assert LARK_CALLS[-1]["op"] == "update" and LARK_CALLS[-1]["id"] == "om_100"
    assert "🤖 **AI note (triggered)**\nnote for triggered: card test" == _ai_note(card)
    assert _buttons(card) == ["Acknowledge", "Resolve / Normal", "📞 Call on duty", "Open in PagerDuty"]  # nothing else changed


def test_duplicate_trigger_event_adds_no_line_and_no_ai_call():
    zoho("triggered")
    assert LARK_CALLS[-1]["op"] == "update" and LARK_CALLS[-1]["id"] == "om_100"
    assert _history(LARK_CALLS[-1]["card"]).count("Triggered") == 1
    assert AI_CALLS == ["triggered"]


def test_escalated_is_orange_same_buttons_and_survives_ai_failure():
    zoho("escalated", agent="")
    card = LARK_CALLS[-1]["card"]
    assert card["header"]["template"] == "orange"
    assert _buttons(card) == ["Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]
    assert "🟠 Escalated" in _history(card)
    # model failed for this state: the card keeps showing the newest note it has, labelled with its stage
    assert AI_CALLS[-1] == "escalated" and _ai_note(card).startswith("🤖 **AI note (triggered)**")


def test_ack_click_answers_at_once_then_repaints_yellow_then_event_is_silent():
    resp = click("acknowledged")
    assert resp == {"toast": {"type": "info", "content": "Acknowledging in PagerDuty…"}}   # no card in the callback
    card = _last("update")["card"]                     # the repaint after PagerDuty confirmed
    assert _last("update")["id"] == "om_100"
    assert card["header"]["template"] == "yellow"
    assert _buttons(card) == ["Escalate", "Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]
    assert "Acknowledged by <at id=ou_me></at>" in _history(card)
    n = len(LARK_CALLS)
    zoho("acknowledged")                      # PagerDuty echoes the click a second later
    assert len(LARK_CALLS) == n + 1           # repainted, but
    assert _history(LARK_CALLS[-1]["card"]).count("Acknowledged") == 1   # no second line


def test_meeting_failure_adds_warning_and_keeps_button():
    def down(topic, owner_open_id, hours=24):
        raise lark_client.LarkError("create_meeting: code=99991663 msg=no permission vc:reserve")
    lark_client.create_meeting = down
    resp = click("meeting")
    assert resp == {"toast": {"type": "info", "content": "Creating the incident meeting…"}}
    card = _last("update")["card"]
    assert card["header"]["template"] == "yellow"                       # state untouched
    assert "⚠️ Meeting by <at id=ou_me></at> failed: create_meeting: code=99991663" in _history(card)
    assert _buttons(card) == ["Escalate", "Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]   # can retry
    assert db.get_pd_card("Q1")["meeting_url"] is None
    lark_client.create_meeting = fake_create_meeting


def test_meeting_click_creates_one_meeting_and_shows_the_link():
    click("meeting")
    assert MEETING_CALLS == [{"topic": "card test", "owner": "ou_me", "hours": config.MEETING_HOURS}]
    card = _last("update")["card"]
    facts = card["elements"][0]["text"]["content"]
    assert "📞 **Meeting:** [Join the incident call](https://vc.larksuite.com/j/1)" in facts
    assert _buttons(card) == ["Escalate", "Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]           # button gone once a link exists
    assert "📞 Meeting created by <at id=ou_me></at>" in _history(card)
    assert db.get_pd_card("Q1")["meeting_url"] == "https://vc.larksuite.com/j/1"
    assert AI_CALLS[-1] == "acknowledged"                               # a meeting is not a state: no AI note
    msg = _last("post_text")                                            # plain text + raw URL → Lark unfurls the meeting widget
    assert msg["chat"] == "oc_test"
    assert msg["text"] == "card test\n\n🚨 Incident meeting created.\n\njoin in meeting link\nhttps://vc.larksuite.com/j/1"
    n_msgs = len([c for c in LARK_CALLS if c["op"] == "post_text"])
    click("meeting", who="ou_other")                                    # second click, other person
    assert len(MEETING_CALLS) == 1                                      # same meeting, no new reservation
    assert len([c for c in LARK_CALLS if c["op"] == "post_text"]) == n_msgs   # and no second message
    assert "https://vc.larksuite.com/j/1" in _last("update")["card"]["elements"][0]["text"]["content"]


def test_escalate_button_moves_pagerduty_to_the_next_level():
    LEVELS = []
    pagerduty.escalate = lambda incident_id, level: (LEVELS.append(level) or dict(PD_INC, status="triggered"))
    resp = click("escalate")
    assert resp["toast"]["content"] == "Escalating in PagerDuty…"
    assert LEVELS == [2]                                               # first press: level 2
    card = _last("update")["card"]
    assert card["header"]["template"] == "orange"
    assert _buttons(card) == ["Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]
    assert "🟠 Escalated by <at id=ou_me></at>" in _history(card)
    zoho("acknowledged")                                               # back to yellow for the tests that follow
    assert _last("update")["card"]["header"]["template"] == "yellow"


ASSIGNED = []
pagerduty.assign = lambda incident_id, user_id: (ASSIGNED.append((incident_id, user_id)) or
                                                 dict(PD_INC, status="acknowledged", assignments=[{"assignee": {"summary": "OSE Duty"}}]))
config.PD_DUTY_USER_ID = "PDUTY01"


def test_call_on_duty_assigns_the_incident_to_the_duty_account():
    n_meetings = len(MEETING_CALLS)
    resp = click("call_duty")
    assert resp["toast"]["content"] == "Handing it to the duty account in PagerDuty…"
    assert ASSIGNED == [("Q1", "PDUTY01")]
    assert len(MEETING_CALLS) == n_meetings                            # no meeting, no Lark call
    card = _last("update")["card"]
    assert "📞 Assigned to the duty account, PagerDuty is calling them, asked by <at id=ou_me></at>" in _history(card)
    assert "**Assigned:** OSE Duty" in card["elements"][0]["text"]["content"]
    assert card["header"]["template"] == "yellow"                      # PagerDuty said still acknowledged


def test_call_on_duty_failure_is_a_warning_line():
    def boom(incident_id, user_id):
        raise pagerduty.PagerDutyError("assign: HTTP 402 Required abilities are unavailable")
    pagerduty.assign = boom
    click("call_duty")
    assert "⚠️ Call on duty by <at id=ou_me></at> failed: assign: HTTP 402" in _history(_last("update")["card"])
    pagerduty.assign = lambda incident_id, user_id: dict(PD_INC, status="acknowledged")


def test_snooze_click_then_ack_event_is_noise():
    resp = click("snooze", option="30")
    assert resp["toast"]["content"] == "Ignoring for 30 min in PagerDuty…"
    card = _last("update")["card"]
    assert card["header"]["template"] == "yellow"
    assert _buttons(card) == ["Escalate", "Resolve / Normal", "📞 Need to call duty?", "Open in PagerDuty"]           # meeting link already on the card
    assert "Join the incident call" in card["elements"][0]["text"]["content"]
    assert "Ignored for 30 min by <at id=ou_me></at>" in _history(card)
    n_ack = _history(card).count("Acknowledged")
    zoho("acknowledged")
    assert _history(LARK_CALLS[-1]["card"]).count("Acknowledged") == n_ack      # the ack event after a snooze adds no line


def test_ignore_window_end_is_announced_once():
    row = db.get_pd_card("Q1")
    assert abs(row["snoozed_until"] - (__import__("time").time() + 30 * 60)) < 5     # set by the snooze click above
    db.upsert_pd_card("Q1", snoozed_until=__import__("time").time() - 1)             # pretend 30 min passed
    assert pipeline.sweep_snoozes() == 1
    assert 'Ignore time for "card test" is over' in _last("post_text")["text"]
    card = _last("update")["card"]
    assert card["header"]["template"] == "yellow"                                     # state only changes on PagerDuty's event
    assert "⏰ Ignore time over" in _history(card)
    assert db.get_pd_card("Q1")["snoozed_until"] == 0
    assert pipeline.sweep_snoozes() == 0                                              # announced once


def test_resolve_from_pagerduty_app_strips_buttons():
    zoho("resolved")
    card = LARK_CALLS[-1]["card"]
    assert card["header"]["template"] == "green"
    assert _buttons(card) == ["Open in PagerDuty"]
    assert "🟢 Resolved by Renzel Hernandez" in _history(card)
    assert db.get_pd_card("Q1")["state"] == "resolved"
    # the meeting link is dropped from the resolved card and the reservation is cancelled in Lark
    assert "Join the incident call" not in card["elements"][0]["text"]["content"]
    assert MEETING_DELETED == ["rsv1"] and db.get_pd_card("Q1")["meeting_reserve_id"] == ""


def test_stale_trigger_after_resolve_is_ignored():
    n = len(LARK_CALLS)
    out = zoho("triggered")
    assert out["card"].startswith("skipped"), out
    assert len(LARK_CALLS) == n
    assert db.get_pd_card("Q1")["state"] == "resolved"
    zoho("reopened")                          # an explicit reopen does go back to red
    assert LARK_CALLS[-1]["card"]["header"]["template"] == "red"
    assert _buttons(LARK_CALLS[-1]["card"]) == ["Acknowledge", "Resolve / Normal", "📞 Call on duty", "Open in PagerDuty"]


def test_click_on_unknown_card_learns_its_message_id():
    pd2 = dict(PD_INC, id="Q2")
    pagerduty.set_status = lambda incident_id, status: dict(pd2, status=status)
    act = {"value": {"action": "resolved", "incident_id": "Q2"}}
    resp = pipeline.handle_card_action({"action": act, "operator": {"open_id": "ou_x"}, "context": {"open_message_id": "om_zoho"}})
    _wait_paint()
    assert resp["toast"]["type"] == "info"
    assert db.get_pd_card("Q2")["lark_message_id"] == "om_zoho"        # learned from the click's context
    pushes = [c for c in LARK_CALLS if c["op"] == "update" and c["id"] == "om_zoho"]
    assert pushes and pushes[0]["card"]["header"]["template"] == "green"   # the click's own repaint
    assert _buttons(pushes[0]["card"]) == ["Open in PagerDuty"]
    assert "note for resolved" in _ai_note(pushes[-1]["card"])          # then the AI note on the same card


def test_pd_refusal_adds_a_warning_line_and_keeps_the_state():
    def boom(incident_id, status):
        raise pagerduty.PagerDutyError("set_status: HTTP 404 Incident Not Found")
    pagerduty.set_status = boom
    n, m = len(LARK_CALLS), len(AI_CALLS)
    before = db.get_pd_card("Q1")["state"]
    click("resolved")
    assert len(LARK_CALLS) == n + 1
    card = LARK_CALLS[-1]["card"]
    assert card["header"]["template"] == cards.STYLE[before]["color"]   # state unchanged
    assert "⚠️ Resolved by <at id=ou_me></at> failed: set_status: HTTP 404" in _history(card)
    assert len(AI_CALLS) == m                           # a failure never triggers a note


def test_double_click_is_ignored_quietly():
    def already(incident_id, status):
        raise pagerduty.PagerDutyError('set_status: HTTP 400 {"errors":["Incident Already Resolved"]}')
    pagerduty.set_status = already
    n = len(LARK_CALLS)
    click("resolved")
    assert len(LARK_CALLS) == n                         # nothing repainted, nothing added


def test_webhook_event_id_is_replaced_by_id_from_url():
    out = zoho("triggered", incident_id="01H2GHGH682DUWOZVZD6JL6O57", html_url="https://x.pagerduty.com/incidents/Q9NEWCARD00001",
               incident_key="k9")
    assert out["pd_incident_id"] == "Q9NEWCARD00001", out
    card = LARK_CALLS[-1]["card"]
    btn = [a for a in card["elements"][-1]["actions"] if a["tag"] == "button"][0]
    assert btn["value"]["incident_id"] == "Q9NEWCARD00001"


def test_missing_incident_key_is_recovered_from_pagerduty():
    inc = db.create_incident(fingerprint="renzel-test", rule_name="Renzel-test", severity="CRITICAL", status="paged",
                             pd_dedup_key="renzel-test-123", chat_id="oc_test", alert_message_id="om_a")
    pagerduty.get_incident_alerts = lambda incident_id: [
        {"alert_key": "renzel-test-123", "body": {"details": {"incident_id": inc["id"], "lark_thread": "om_a"}}}]
    out = zoho("resolved", incident_id="Q7REAL00000001", html_url="https://x.pagerduty.com/incidents/Q7REAL00000001", incident_key="")
    assert out["incident_id"] == inc["id"], out
    assert db.get_incident(int(inc["id"]))["status"] == "resolved"
    results = [o["result"] for o in db._all(db._conn().execute("select result from outcomes where incident_id=?", (inc["id"],)))]
    assert "pd_resolved" in results
    assert "ai_summary" in results                       # the resolved note feeds past_cases
    assert db.get_pd_card("Q7REAL00000001")["incident_id"] == inc["id"]
    assert "note for resolved" in _ai_note(LARK_CALLS[-1]["card"])


def test_forged_status_for_nonexistent_incident_posts_nothing():
    def not_found(incident_id):
        raise pagerduty.PagerDutyError("get_incident: HTTP 404 Incident Not Found")
    real = pagerduty.get_incident
    pagerduty.get_incident = not_found
    n = len(LARK_CALLS)
    out = zoho("triggered", incident_id="QFAKE000000001", html_url="https://x.pagerduty.com/incidents/QFAKE000000001")
    pagerduty.get_incident = real
    assert out["ok"] is False and out["card"].startswith("rejected"), out
    assert len(LARK_CALLS) == n and db.get_pd_card("QFAKE000000001") is None


def test_unknown_event_is_skipped():
    out = zoho("annotated")
    assert out["card"] == "skipped"


def test_lark_failure_does_not_raise():
    def broken(message_id, card):
        raise lark_client.LarkError("update_card: code=230011")
    lark_client.update_card = broken
    out = zoho("resolved", incident_id="Q1")
    assert out["card"] == "painted"     # PagerDuty is the truth; Lark trouble is only logged


if __name__ == "__main__":
    import sys
    # definition order, not alphabetical: the tests walk one incident through its life
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
