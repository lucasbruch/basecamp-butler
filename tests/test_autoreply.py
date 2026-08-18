"""Auto-reply gating.

This is the only path in the app that says something to another person, so the
tests here are mostly about the times it must stay silent: an unlisted sender, a
conversation it already answered, a thread it has never seen before, the moment
the LLM drops out. The one case that produces a message is the last test.

The LLM itself is stubbed — what's under test is the decision to speak, not the
sentence.
"""
from datetime import timedelta

import pytest

from app import autoreply, runtime
from app.classifier import ollama
from app.models import AppState, AutoReply, AutoReplyRule

from helpers import identity, make_event

MY_ID = 99


def cfg(**over):
    return runtime.RuntimeConfig(
        **{**runtime.defaults(), "autoreply_enabled": True, **over}
    )


def rule(db, name="Ana", mode="draft", enabled=True, tone="warm but brief"):
    row = AutoReplyRule(name=name, tone=tone, mode=mode, enabled=enabled)
    db.add(row)
    db.flush()
    return row


def ping(db, *, body="can you send the budget?", who="Ana", who_id=2, chat_id=7,
         basecamp_id=1, event_id=None):
    """A ping line as the poller stores it (Circles have no project id)."""
    return make_event(
        db, etype="ping", project_id=None, chat_id=chat_id, body=body,
        who=who, who_id=who_id, basecamp_id=basecamp_id, event_id=event_id,
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
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, event_id=10)
    assert run(db) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db) == 0
    assert db.query(AutoReply).count() == 1


def test_cooldown_of_zero_disables_the_window(db, spoken):
    identity(db, MY_ID)
    rule(db)
    watermark(db)
    ping(db, event_id=10)
    assert run(db, autoreply_cooldown_minutes=0) == 1
    ping(db, event_id=11, basecamp_id=2, body="and the schedule too?")
    assert run(db, autoreply_cooldown_minutes=0) == 1


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
