"""The self-check, and the rewind that recovers a stuck conversation.

Every finding here exists because the failure it describes was, at some point,
indistinguishable from "the feature doesn't work": a name that doesn't match
Basecamp's spelling, a rule left in draft mode, a thread whose messages an older
build marked handled without ever answering them.
"""
from datetime import timedelta

import pytest

from app import autoreply, runtime
from app.models import AppState, AutoReply, AutoReplyRule, RawEvent

from helpers import ago, identity, make_event

MY_ID = 99


def cfg(**over):
    return runtime.RuntimeConfig(
        **{**runtime.defaults(), "autoreply_enabled": True,
           "quiet_hours_start": 0, "quiet_hours_end": 0, **over}
    )


@pytest.fixture(autouse=True)
def fixed_config(monkeypatch):
    """Every check reads the runtime config; pin it unless a test says otherwise."""
    monkeypatch.setattr(runtime, "load", lambda _db: cfg())


def rule(db, name="Ana", mode="auto", enabled=True):
    row = AutoReplyRule(name=name, mode=mode, enabled=enabled)
    db.add(row)
    db.flush()
    return row


def ping(db, *, who="Ana", who_id=2, chat_id=7, basecamp_id=1, event_id=None,
         when=None):
    return make_event(
        db, etype="ping", project_id=None, chat_id=chat_id, body="hello?",
        who=who, who_id=who_id, basecamp_id=basecamp_id, event_id=event_id,
        when=when, _circle_id=555,
    )


def levels(findings, level):
    return [f["text"] for f in findings if f["level"] == level]


def text(findings):
    return " | ".join(f["text"] for f in findings)


# ── who has actually been pinging ───────────────────────────────────────────
def test_it_reports_the_names_basecamp_actually_used(db):
    """The one fact nothing in the UI ever showed, and the one a rule is
    matched against."""
    identity(db, MY_ID)
    ping(db, who="Ana Müller", event_id=10)
    ping(db, who="Tom", who_id=3, chat_id=8, basecamp_id=2, event_id=11)
    assert autoreply.recent_senders(db) == ["Tom", "Ana Müller"]


def test_your_own_lines_are_not_senders(db):
    identity(db, MY_ID)
    ping(db, who="Sam", who_id=MY_ID, event_id=10)
    assert autoreply.recent_senders(db) == []


def test_a_name_that_does_not_match_is_called_out(db):
    """"Ana" against an account that displays "Ana Müller" answers nobody, and
    looks exactly like a broken feature."""
    identity(db, MY_ID)
    rule(db, name="Ana")
    ping(db, who="Ana Müller", event_id=10)
    found = autoreply.self_check(db)
    assert "Ana Müller" in text(found)
    assert any("isn't on the list" in w for w in levels(found, "warn"))
    assert any("nobody spelled that way" in w for w in levels(found, "warn"))


def test_a_matching_name_is_reported_as_working(db):
    identity(db, MY_ID)
    rule(db, name="ana")  # case doesn't matter
    ping(db, who="Ana", event_id=10)
    found = autoreply.self_check(db)
    assert levels(found, "problem") == []
    assert any("answer automatically" in ok for ok in levels(found, "ok"))


def test_draft_mode_is_flagged_because_it_looks_like_silence(db):
    """A draft-mode rule *does* reply — onto this page. From Basecamp it is
    indistinguishable from not replying at all."""
    identity(db, MY_ID)
    rule(db, name="Ana", mode="draft")
    ping(db, who="Ana", event_id=10)
    assert any("draft* mode" in w for w in levels(autoreply.self_check(db), "warn"))


# ── the things that stop it dead ────────────────────────────────────────────
def test_switched_off_is_the_first_problem(db):
    identity(db, MY_ID)
    rule(db)
    ping(db, event_id=10)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runtime, "load", lambda _db: cfg(autoreply_enabled=False))
        found = autoreply.self_check(db)
    assert "switched off" in levels(found, "problem")[0]


def test_an_empty_list_is_a_problem_not_a_warning(db):
    identity(db, MY_ID)
    ping(db, event_id=10)
    assert any("Nobody is on the auto-reply list" in p
               for p in levels(autoreply.self_check(db), "problem"))


def test_every_rule_disabled_is_a_problem(db):
    identity(db, MY_ID)
    rule(db, name="Ana", enabled=False)
    ping(db, who="Ana", event_id=10)
    assert any("Every name on the list is disabled" in p
               for p in levels(autoreply.self_check(db), "problem"))


def test_no_pings_at_all_points_upstream(db):
    """Nothing to answer is a polling problem, and saying so stops the search
    happening in the wrong module."""
    identity(db, MY_ID)
    rule(db)
    found = autoreply.self_check(db)
    assert any("aren't reaching the app" in p for p in levels(found, "problem"))


def test_an_unknown_identity_is_a_problem(db):
    """Without it the butler can't tell its own lines from theirs."""
    rule(db)
    ping(db, event_id=10)
    assert any("who you are" in p for p in levels(autoreply.self_check(db), "problem"))


def test_an_unreachable_llm_is_a_problem(db):
    identity(db, MY_ID)
    rule(db)
    ping(db, event_id=10)
    db.merge(AppState(key="llm_status", value="unreachable"))
    db.flush()
    assert any("isn't answering" in p for p in levels(autoreply.self_check(db), "problem"))


def test_quiet_hours_and_the_ceiling_are_warnings_not_faults(db, monkeypatch):
    identity(db, MY_ID)
    rule(db, name="Ana")
    ping(db, who="Ana", event_id=10)
    db.add(AutoReply(draft="earlier", status="sent", chat_id=9,
                     sent_at=autoreply.utcnow()))
    db.flush()
    monkeypatch.setattr(
        runtime, "load",
        lambda _db: cfg(quiet_hours_start=22, quiet_hours_end=7,
                        autoreply_daily_limit=1),
    )
    monkeypatch.setattr(
        runtime.RuntimeConfig, "is_quiet_now", lambda self, now=None: True
    )
    found = autoreply.self_check(db)
    assert levels(found, "problem") == []
    assert any("quiet hours" in w for w in levels(found, "warn"))
    assert any("daily ceiling" in w for w in levels(found, "warn"))


def test_a_clean_bill_says_so(db):
    identity(db, MY_ID)
    rule(db, name="Ana")
    ping(db, who="Ana", event_id=10)
    assert any("Nothing is standing in the way" in ok
               for ok in levels(autoreply.self_check(db), "ok"))


# ── the rewind ──────────────────────────────────────────────────────────────
def test_a_stuck_conversation_can_be_put_back_in_view(db, monkeypatch):
    """The recovery path for lines an older build marked handled without ever
    answering them: a watermark is one-way, so nothing else brings them back."""
    monkeypatch.setattr(autoreply, "session_scope", _scope(db))
    identity(db, MY_ID)
    ping(db, event_id=10)
    ping(db, event_id=11, basecamp_id=2)
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}7", value="11"))
    db.flush()

    ok, message = autoreply.reconsider(7)
    assert ok and "read that conversation again" in message
    assert autoreply._watermark(db, 7) == 9  # both lines are fresh again


def test_a_rewind_will_not_reach_past_the_answerable_window(db, monkeypatch):
    monkeypatch.setattr(autoreply, "session_scope", _scope(db))
    identity(db, MY_ID)
    ping(db, event_id=10, when=ago(hours=autoreply.STALE_AFTER_HOURS + 2))
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}7", value="10"))
    db.flush()

    ok, message = autoreply.reconsider(7)
    assert not ok
    assert "nothing left to look at" in message
    assert autoreply._watermark(db, 7) == 10  # untouched


def test_a_rewind_records_what_it_did(db, monkeypatch):
    monkeypatch.setattr(autoreply, "session_scope", _scope(db))
    identity(db, MY_ID)
    ping(db, event_id=10)
    db.merge(AppState(key=f"{autoreply.CP_PREFIX}7", value="10"))
    db.flush()

    assert autoreply.reconsider(7)[0]
    why = [d["why"] for d in autoreply.decisions(db) if d["chat_id"] == "7"]
    assert why and "by hand" in why[0]


def _scope(session):
    """`reconsider` opens its own session; hand it the test's instead."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session

    return scope
