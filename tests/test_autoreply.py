"""Auto-reply gating.

This is the only path in the app that says something to another person, so the
tests here are mostly about the times it must stay silent: an unlisted sender, a
conversation it already answered, a thread it has never seen before, the moment
the LLM drops out. The one case that produces a message is the last test.

The LLM itself is stubbed — what's under test is the decision to speak, not the
sentence.
"""
import json
from datetime import timedelta

import pytest

from app import autoreply, runtime
from app.classifier import ollama
from app.models import ActivityLog, AppState, AutoReply, AutoReplyRule

from helpers import ago, identity, make_event

MY_ID = 99


def cfg(**over):
    return runtime.RuntimeConfig(
        **{**runtime.defaults(), "autoreply_enabled": True, **over}
    )


def rule(db, name="Ana", mode="draft", enabled=True, tone="warm but brief",
         chat_id=7):
    """A rule pointed at the same conversation `ping()` writes into by default.

    A rule names the one Ping it may speak in, so a rule with no `chat_id` says
    nothing anywhere — which is a case worth testing, not the setup for every
    other test."""
    row = AutoReplyRule(name=name, tone=tone, mode=mode, enabled=enabled,
                        chat_id=chat_id)
    db.add(row)
    db.flush()
    return row


def ping(db, *, body="can you send the budget?", who="Ana", who_id=2, chat_id=7,
         basecamp_id=1, event_id=None, when=None):
    """A ping line as the poller stores it (Circles have no project id)."""
    return make_event(
        db, etype="ping", project_id=None, chat_id=chat_id, body=body,
        who=who, who_id=who_id, basecamp_id=basecamp_id, event_id=event_id,
        when=when,
        _circle_id=555, app_url="https://3.basecamp.com/1/buckets/555/chats/7",
    )


def watermark(db, chat_id=7, value=0):
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}{chat_id}", value=str(value)))
    db.flush()


@pytest.fixture
def spoken(monkeypatch):
    """Capture what would have been posted to Basecamp, and answer for the LLM."""
    sent: list[tuple] = []

    class FakeClient:
        def create_chat_line(self, bucket_id, chat_id, content):
            sent.append((bucket_id, chat_id, content))
            return {"id": 4242, "app_url": "https://3.basecamp.com/1/x"}

        def close(self):
            pass

    monkeypatch.setattr(autoreply, "client_for", lambda db: FakeClient())
    monkeypatch.setattr(
        ollama,
        "draft_reply",
        lambda transcript, **kw: {
            "reply": True, "text": "On it — back to you today.", "why": "asked", "prompt": "",
        },
    )
    return sent


def run(db, **over):
    """Drive one pass with a given config, bypassing `run_pass`'s own session."""
    saved = runtime.load
    runtime.load = lambda _db: cfg(**over)
    try:
        return autoreply._run(db)
    finally:
        runtime.load = saved


# ── staying quiet ───────────────────────────────────────────────────────────
def test_does_nothing_when_switched_off(db, spoken):
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, event_id=10)
    assert run(db, autoreply_enabled=False) == 0
    assert spoken == []


def test_does_nothing_without_any_rule(db, spoken):
    """No allowlist entry means no reply — there is no 'answer everyone' mode."""
    identity(db, MY_ID)
    watermark(db)
    ping(db, event_id=10)
    assert run(db) == 0
    assert spoken == []


def test_ignores_a_sender_who_is_not_on_the_list(db, spoken):
    identity(db, MY_ID)
    rule(db, name="Ana")
    watermark(db)
    ping(db, who="Someone Else", who_id=3, event_id=10)
    assert run(db) == 0
    assert db.query(AutoReply).count() == 0


def test_a_disabled_rule_does_not_reply(db, spoken):
    identity(db, MY_ID)
    rule(db, name="Ana", enabled=False)
    watermark(db)
    ping(db, event_id=10)
    assert run(db) == 0


def test_first_sight_of_a_thread_only_seeds_a_watermark(db, spoken):
    """Switching the feature on must not fire replies at a week of history."""
    identity(db, MY_ID)
    rule(db)
    ping(db, event_id=10)
    assert run(db) == 0
    assert db.query(AutoReply).count() == 0
    assert autoreply._watermark(db, 7) == 10
    # …and the *next* message in that thread is answerable.
    ping(db, event_id=11, basecamp_id=2)
    assert run(db) == 1


def test_our_own_line_being_last_means_nothing_to_answer(db, spoken):
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, event_id=10)
    ping(db, event_id=11, basecamp_id=2, who="Sam", who_id=MY_ID, body="already on it")
    assert run(db) == 0
    assert db.query(AutoReply).count() == 0


def test_cooldown_suppresses_a_second_reply_to_the_same_thread(db, spoken):
    """A burst of five messages is one exchange — but the ones it doesn't answer
    are *held*, not thrown away. The watermark staying put is the whole point:
    moving it here is what used to make the butler reply exactly once and then
    go quiet for the rest of the conversation."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0
    assert db.query(AutoReply).count() == 1
    assert autoreply._watermark(db, 7) == 10  # the follow-up is still on the books


def test_the_follow_up_is_answered_once_the_cooldown_passes(db, monkeypatch, spoken):
    """...and this is what "held" has to mean: it gets answered later."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0

    # Age the send out of the cooldown window; nothing else changes.
    db.query(AutoReply).one().sent_at = autoreply.utcnow() - timedelta(hours=2)
    db.flush()
    monkeypatch.setattr(
        ollama, "draft_reply",
        lambda transcript, **kw: {
            "reply": True, "text": "Schedule follows tonight.", "why": "asked",
            "prompt": "",
        },
    )
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    assert db.query(AutoReply).count() == 2
    assert autoreply._watermark(db, 7) == 11


def test_a_waiting_draft_holds_the_next_message_rather_than_dropping_it(db, spoken):
    """One unread draft per conversation. The newer lines wait behind it instead
    of being consumed while you weren't looking."""
    identity(db, MY_ID)
    rule(db, mode="draft")
    watermark(db)
    ping(db, event_id=10)
    assert run(db) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db) == 0
    assert db.query(AutoReply).count() == 1
    assert autoreply._watermark(db, 7) == 10

    # Deal with what's waiting and the newer line becomes answerable.
    db.query(AutoReply).one().status = "discarded"
    db.flush()
    assert run(db) == 1


def test_cooldown_of_zero_disables_the_window(db, monkeypatch, spoken):
    """With the window off, a second question gets a second answer — as long as
    the answer is a different one (see the duplicate tests below). An `auto`
    rule, so the first reply is sent rather than left waiting as a draft."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    drafts = iter(["On it — back to you today.", "Schedule follows this evening."])
    monkeypatch.setattr(
        ollama, "draft_reply",
        lambda transcript, **kw: {
            "reply": True, "text": next(drafts), "why": "asked", "prompt": "",
        },
    )
    quiet = {"quiet_hours_start": 0, "quiet_hours_end": 0}
    ping(db, event_id=10)
    assert run(db, autoreply_cooldown_minutes=0, **quiet) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db, autoreply_cooldown_minutes=0, **quiet) == 1


def test_a_line_nobody_answered_in_time_is_left_alone(db, spoken):
    """Deferring can't be unbounded: turning up six hours late, in your name,
    is worse than staying quiet."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10, when=ago(hours=autoreply.STALE_AFTER_HOURS + 1))
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0
    assert spoken == []
    assert autoreply._watermark(db, 7) == 10  # and it stops being reconsidered


def test_the_draft_budget_leaves_a_thread_for_the_next_pass(db, monkeypatch, spoken):
    """A pass caps how many LLM round trips it makes. The threads it doesn't get
    to must keep their place — capping by *thread* meant the same conversations
    were picked every pass and the rest were never looked at at all."""
    monkeypatch.setattr(autoreply, "MAX_DRAFTS_PER_PASS", 1)
    identity(db, MY_ID)
    # A rule speaks in one conversation, so two threads means two rules.
    rule(db, name="Ana", chat_id=7, mode="auto")
    rule(db, name="Bo", chat_id=8, mode="auto")
    watermark(db, chat_id=7)
    watermark(db, chat_id=8)
    ping(db, event_id=10, chat_id=7, who="Ana")
    ping(db, event_id=11, chat_id=8, basecamp_id=2, who="Bo", who_id=3)

    # Most recently active first, so thread 8 spends the budget.
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    assert autoreply._watermark(db, 8) == 11
    assert autoreply._watermark(db, 7) == 0
    # ...and thread 7 is genuinely next, not skipped.
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    assert autoreply._watermark(db, 7) == 10


def test_an_unlisted_sender_gets_a_word_in_the_activity_feed(db, spoken):
    """A rule whose name doesn't match Basecamp's spelling and a feature that is
    simply broken look identical from the UI unless something says so."""
    identity(db, MY_ID)
    rule(db, name="Ana Müller")  # the ping is from plain "Ana"
    watermark(db)
    ping(db, event_id=10)
    def notices():
        return [
            row.summary for row in db.query(ActivityLog).all()
            if "auto-reply list" in row.summary
        ]

    assert run(db) == 0
    assert len(notices()) == 1
    assert "Ana" in notices()[0]

    # ...but only now and then: this is the common case, not an incident.
    ping(db, event_id=11, basecamp_id=2, body="still there?")
    assert run(db) == 0
    assert len(notices()) == 1


def test_an_unreachable_llm_leaves_the_thread_for_next_time(db, monkeypatch, spoken):
    """Silence is the safe answer to an outage — and the message isn't lost."""
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, event_id=10)
    monkeypatch.setattr(ollama, "draft_reply", lambda transcript, **kw: ollama.UNREACHABLE)
    assert run(db) == 0
    assert autoreply._watermark(db, 7) == 0  # not advanced
    assert db.query(AutoReply).count() == 0


def test_a_model_that_declines_writes_no_draft(db, monkeypatch, spoken):
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, body="thanks!", event_id=10)
    monkeypatch.setattr(
        ollama, "draft_reply",
        lambda transcript, **kw: {"reply": False, "text": "", "why": "just thanks", "prompt": ""},
    )
    assert run(db) == 0
    assert db.query(AutoReply).count() == 0
    assert autoreply._watermark(db, 7) == 10  # but we won't reconsider it


# ── holding back an "auto" rule ─────────────────────────────────────────────
def test_auto_mode_sends(db, spoken):
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    reply = db.query(AutoReply).one()
    assert reply.status == "sent"
    assert spoken and spoken[0][0] == 555 and spoken[0][1] == 7


def test_quiet_hours_hold_an_auto_reply_as_a_draft(db, spoken, monkeypatch):
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    monkeypatch.setattr(
        runtime.RuntimeConfig, "is_quiet_now", lambda self, now=None: True
    )
    assert run(db) == 1
    reply = db.query(AutoReply).one()
    assert reply.status == "draft"
    assert "quiet hours" in reply.held_reason
    assert spoken == []


def test_a_reply_held_over_quiet_hours_goes_out_when_they_end(db, spoken):
    """An `auto` rule means "don't show me first"; quiet hours decide *when* it
    speaks, not whether. A held draft that then waits for a click is exactly the
    outcome the rule was set up to avoid."""
    identity(db, MY_ID)
    row = rule(db, mode="auto")
    held = AutoReply(
        draft="Morning — on it.", status="draft", mode="auto", chat_id=7,
        circle_id=555, person="Ana", rule_id=row.id,
        held_reason=autoreply.HELD_QUIET,
    )
    db.add(held)
    db.flush()
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0
    assert held.status == "sent"
    assert held.held_reason is None
    assert spoken and spoken[0][1] == 7


def test_a_rule_switched_off_overnight_keeps_its_held_reply_in(db, spoken):
    """Turning the rule off during the night means what it says. The draft is
    still there to read; it just doesn't leave on its own."""
    identity(db, MY_ID)
    row = rule(db, mode="auto", enabled=False)
    held = AutoReply(
        draft="Morning — on it.", status="draft", mode="auto", chat_id=7,
        circle_id=555, person="Ana", rule_id=row.id,
        held_reason=autoreply.HELD_QUIET,
    )
    db.add(held)
    db.flush()
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0
    assert held.status == "draft"
    assert spoken == []


def test_a_reply_held_by_the_daily_ceiling_stays_held(db, spoken):
    """A ceiling that empties itself an hour later isn't one."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    held = AutoReply(
        draft="Later.", status="draft", mode="auto", chat_id=7, circle_id=555,
        person="Ana", held_reason="daily limit of 1 auto-replies reached",
    )
    db.add(held)
    db.flush()
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 0
    assert held.status == "draft"
    assert spoken == []


def test_the_daily_ceiling_holds_an_auto_reply_as_a_draft(db, spoken):
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    # One already sent today, with a limit of one.
    db.add(AutoReply(draft="earlier", status="sent", chat_id=8,
                     sent_at=autoreply.utcnow() - timedelta(hours=1)))
    db.flush()
    ping(db, event_id=10)
    assert run(db, autoreply_daily_limit=1, quiet_hours_start=0, quiet_hours_end=0) == 1
    reply = db.query(AutoReply).filter(AutoReply.status == "draft").one()
    assert "daily limit" in reply.held_reason
    assert spoken == []


def test_draft_mode_never_sends(db, spoken):
    identity(db, MY_ID)
    rule(db, mode="draft")
    watermark(db)
    ping(db, event_id=10)
    assert run(db, quiet_hours_start=0, quiet_hours_end=0) == 1
    assert db.query(AutoReply).one().status == "draft"
    assert spoken == []


# ── what actually goes on the wire ──────────────────────────────────────────
def test_the_reply_is_html_escaped(db):
    """The body is model output built from someone else's message; a stray '<'
    would eat the rest of the line, and anything sharper would post as markup."""
    posted = autoreply.as_html('5 < 6 & "quoted"\nsecond line')
    assert "<br>" in posted
    assert "&lt; 6 &amp;" in posted
    assert "<script" not in autoreply.as_html("<script>alert(1)</script>")


def test_apostrophes_arrive_as_apostrophes(db):
    """Basecamp shows an escaped quote as the entity itself, so "it's" has to go
    out as "it's" — the characters that need escaping are the structural ones."""
    posted = autoreply.as_html("it's fine, she said \"yes\"")
    assert posted == 'it\'s fine, she said "yes"'
    assert "&#x27;" not in posted and "&quot;" not in posted


# ── never the same message twice ────────────────────────────────────────────
def test_it_will_not_draft_a_line_it_has_already_sent(db, spoken):
    """The model can only see the conversation, and its own last reply is part
    of it — asked again once the thread has moved on to small talk, it hands
    back that same sentence. Saying it a second time is what the other person
    notices, so the pass stops there."""
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    db.add(AutoReply(draft="On it — back to you today.", status="sent", chat_id=7,
                     sent_at=autoreply.utcnow() - timedelta(hours=1)))
    db.flush()
    ping(db, who="Ana", body="hahaha", event_id=10)
    assert run(db, autoreply_cooldown_minutes=0, quiet_hours_start=0,
               quiet_hours_end=0) == 0
    assert spoken == []
    assert db.query(AutoReply).count() == 1          # no second row
    assert autoreply._watermark(db, 7) == 10         # and we move on


def test_whitespace_and_case_do_not_make_it_a_new_message(db):
    db.add(AutoReply(draft="On it — back to you today.", status="sent", chat_id=7))
    db.flush()
    assert autoreply.already_said(db, 7, "on it —  back to  you TODAY.")
    assert not autoreply.already_said(db, 7, "Something else entirely.")
    assert not autoreply.already_said(db, 8, "On it — back to you today.")


def test_delivery_refuses_to_post_the_same_text_twice(db, spoken):
    """The last-ditch guard: whatever route got us here — a retried pass, a
    second click on Send, a transaction that rolled back after the message had
    already left — Basecamp doesn't get it again."""
    identity(db, MY_ID)
    db.add(AutoReply(draft="On it — back to you today.", status="sent", chat_id=7,
                     circle_id=555, sent_at=autoreply.utcnow()))
    again = AutoReply(draft="On it — back to you today.", status="draft",
                      chat_id=7, circle_id=555, person="Ana")
    db.add(again)
    db.flush()
    assert autoreply._deliver(db, again) is False
    assert spoken == []
    assert again.status == "draft"                   # still there to look at
    assert "already in this conversation" in again.error


def test_it_replies_to_the_conversation_it_read(db, spoken):
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    run(db, quiet_hours_start=0, quiet_hours_end=0)
    bucket_id, chat_id, content = spoken[0]
    assert (bucket_id, chat_id) == (555, 7)     # the Circle, not a project
    assert "back to you today" in content
    reply = db.query(AutoReply).one()
    assert reply.person == "Ana"
    assert reply.basecamp_line_id == 4242


# ── saying why it stayed quiet ──────────────────────────────────────────────
def why(db, chat_id=7):
    """The recorded decision for one conversation, or None."""
    for row in autoreply.decisions(db):
        if row["chat_id"] == str(chat_id):
            return row["why"]
    return None


def test_it_records_why_it_said_nothing(db, spoken):
    """Every branch that stays quiet has to leave a reason behind. A butler that
    is silent because the sender isn't on the list and one that is silent
    because it is broken look identical otherwise."""
    identity(db, MY_ID)
    rule(db, name="Ana Müller")  # the ping is from plain "Ana"
    watermark(db)
    ping(db, event_id=10)
    run(db)
    assert "isn't on the auto-reply list" in why(db)


def test_the_cooldown_says_it_is_holding_not_ignoring(db, spoken):
    identity(db, MY_ID)
    rule(db, mode="auto")
    watermark(db)
    ping(db, event_id=10)
    run(db, quiet_hours_start=0, quiet_hours_end=0)
    assert why(db) == "Replied."
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    run(db, quiet_hours_start=0, quiet_hours_end=0)
    assert "held until it passes" in why(db)


def test_a_declining_model_says_what_it_decided(db, monkeypatch, spoken):
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, body="thanks!", event_id=10)
    monkeypatch.setattr(
        ollama, "draft_reply",
        lambda transcript, **kw: {"reply": False, "text": "", "why": "just thanks",
                                  "prompt": ""},
    )
    run(db)
    assert "no reply was needed: just thanks" in why(db)


def test_a_quiet_pass_says_so_at_the_top_level(db, spoken):
    identity(db, MY_ID)
    rule(db)
    assert run(db) == 0
    assert "No Ping messages arrived" in autoreply.last_pass(db)["why"]


def test_a_switched_off_butler_says_that_rather_than_nothing(db, spoken):
    identity(db, MY_ID)
    rule(db)
    assert run(db, autoreply_enabled=False) == 0
    assert "switched off" in autoreply.last_pass(db)["why"]


def test_an_empty_allowlist_says_that_rather_than_nothing(db, spoken):
    identity(db, MY_ID)
    assert run(db) == 0
    assert "Nobody is on the auto-reply list" in autoreply.last_pass(db)["why"]


def test_nothing_new_leaves_the_previous_reason_standing(db, spoken):
    """A pass where nothing happened must not overwrite the answer to "why did
    you not reply to that message?" with "nothing happened"."""
    identity(db, MY_ID)
    rule(db, name="Ana Müller")
    watermark(db)
    ping(db, event_id=10)
    run(db)
    first = why(db)
    run(db)  # no new lines
    assert why(db) == first


def test_decisions_about_dead_conversations_are_forgotten(db, spoken):
    """One row per conversation, kept for a week — this is a status board, not
    an audit trail, and threads that ended months ago shouldn't crowd it."""
    old = autoreply.utcnow() - timedelta(days=autoreply.WHY_KEEP_DAYS + 1)
    db.merge(AppState(key=f"{autoreply.WHY_PREFIX}7", value=json.dumps(
        {"person": "Ana", "why": "Replied.", "at": old.isoformat()}
    )))
    autoreply._note(db, 8, "Bo", "Replied.")
    assert [d["chat_id"] for d in autoreply.decisions(db)] == ["8"]


def test_a_hand_edited_decision_row_does_not_break_the_page(db, spoken):
    db.merge(AppState(key=f"{autoreply.WHY_PREFIX}7", value="not json"))
    db.flush()
    assert autoreply.decisions(db) == []


# ── which conversation ──────────────────────────────────────────────────────
# A Ping isn't always a direct message: Basecamp lets you start one with several
# people in it, and the allowlist matches the *sender*. So a colleague on the
# list saying something in a five-person Ping used to be answered in your voice
# in front of all five. A rule now names the one conversation it may speak in,
# which is decided rather than inferred — a group where only one person has
# spoken is indistinguishable from a 1:1, so there is nothing safe to infer.
def test_a_rule_with_no_conversation_says_nothing(db, spoken):
    """The state a rule is in before you point it anywhere. Silence is the only
    safe way to be half-configured."""
    identity(db, MY_ID)
    rule(db, name="Ana", chat_id=None)
    watermark(db)
    ping(db, event_id=10)
    assert run(db) == 0
    assert spoken == []
    assert db.query(AutoReply).count() == 0
    assert "hasn't been pointed at a conversation" in why(db)


def test_a_rule_does_not_speak_in_another_conversation(db, spoken):
    """Ana is answered in her own thread and nowhere else — the same person in a
    group Ping is a different room, not a different sender."""
    identity(db, MY_ID)
    rule(db, name="Ana", chat_id=7)
    watermark(db, chat_id=9)
    ping(db, event_id=10, chat_id=9, body="both of you — where are we?")
    assert run(db) == 0
    assert spoken == []
    assert "different conversation" in why(db, chat_id=9)


def test_the_named_conversation_is_answered(db, spoken):
    identity(db, MY_ID)
    rule(db, name="Ana", chat_id=7)
    watermark(db, chat_id=7)
    ping(db, event_id=10, chat_id=7)
    assert run(db) == 1


def test_the_wrong_conversation_does_not_come_back_every_pass(db, spoken):
    """Final decision, so the watermark moves — otherwise every pass rewrites the
    same note about the same lines for as long as the thread exists."""
    identity(db, MY_ID)
    rule(db, name="Ana", chat_id=7)
    watermark(db, chat_id=9)
    ping(db, event_id=10, chat_id=9)
    run(db)
    assert autoreply._watermark(db, 9) == 10


def test_an_unpointed_rule_does_not_come_back_every_pass(db, spoken):
    identity(db, MY_ID)
    rule(db, name="Ana", chat_id=None)
    watermark(db)
    ping(db, event_id=10)
    run(db)
    assert autoreply._watermark(db, 7) == 10


# ── the conversation picker ─────────────────────────────────────────────────
def test_conversations_are_listed_by_who_has_spoken_in_them(db):
    """A chat id on its own is unrecognisable — the names are how you tell your
    1:1 with someone from the group thread that also has them in it."""
    identity(db, MY_ID)
    ping(db, chat_id=7, who="Ana", who_id=2, event_id=10)
    ping(db, chat_id=7, who="Sam", who_id=MY_ID, event_id=11, basecamp_id=2)
    ping(db, chat_id=9, who="Ana", who_id=2, event_id=12, basecamp_id=3)
    ping(db, chat_id=9, who="Bo", who_id=3, event_id=13, basecamp_id=4)

    found = {c["chat_id"]: c for c in autoreply.known_conversations(db)}
    assert found[7]["label"] == "Ana"        # your own lines aren't listed
    assert found[9]["label"] == "Ana, Bo"    # and this is the room


def test_conversations_come_back_most_recent_first(db):
    identity(db, MY_ID)
    ping(db, chat_id=7, event_id=10, when=ago(hours=5))
    ping(db, chat_id=9, event_id=11, basecamp_id=2, when=ago(hours=1))
    assert [c["chat_id"] for c in autoreply.known_conversations(db)] == [9, 7]


def test_a_conversation_only_you_have_spoken_in_says_so(db):
    """Offered rather than hidden — it's still a real conversation — but it must
    not appear as a blank row in the picker."""
    identity(db, MY_ID)
    ping(db, chat_id=7, who="Sam", who_id=MY_ID, event_id=10)
    (only,) = autoreply.known_conversations(db)
    assert only["who"] == []
    assert only["label"] == "nobody but you has spoken here"


def test_conversations_older_than_the_scan_window_are_left_out(db):
    identity(db, MY_ID)
    ping(db, chat_id=7, event_id=10,
         when=ago(days=autoreply.CONVERSATION_SCAN_DAYS + 1))
    assert autoreply.known_conversations(db) == []
