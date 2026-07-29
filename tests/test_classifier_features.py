"""Sender muting, the new recording types, and learning from your own verdicts."""
from app.classifier import ollama, rules
from app.models import MutedSender, Todo

from helpers import identity, make_event, make_todo


# ── muted senders ────────────────────────────────────────────────────────────
def test_muted_sender_raises_nothing(db):
    """The deploy bot posting to Campfire all day is the single biggest source
    of keyword-gate false positives."""
    identity(db)
    db.add(MutedSender(name="Deploy Bot"))
    db.flush()
    make_event(db, etype="ping", chat_id=7, who="Deploy Bot",
               body="please review the release build")
    assert rules.classify_events(db) == []


def test_mute_matching_is_case_insensitive(db):
    identity(db)
    db.add(MutedSender(name="deploy bot"))
    db.flush()
    make_event(db, etype="ping", chat_id=7, who="Deploy BOT",
               body="please review the release build")
    assert rules.classify_events(db) == []


def test_unmuted_sender_still_raises(db):
    identity(db)
    db.add(MutedSender(name="Deploy Bot"))
    db.flush()
    make_event(db, etype="ping", chat_id=7, who="Ana",
               body="please review the release build")
    assert len(rules.classify_events(db)) == 1


def test_mute_applies_to_messages_too(db):
    identity(db)
    db.add(MutedSender(name="Noisy Nigel"))
    db.flush()
    make_event(db, etype="message", chat_id=None, who="Noisy Nigel",
               body="please review the budget document")
    assert rules.classify_events(db) == []


# ── new recording types ──────────────────────────────────────────────────────
def test_meeting_you_are_invited_to_becomes_a_todo(db):
    identity(db, user_id=99)
    make_event(
        db, etype="schedule", chat_id=None, basecamp_id=5, body="",
        title="Grading review", starts_at="2026-08-03T14:00:00.000Z",
        participants=[{"id": 99, "name": "Sam"}, {"id": 2, "name": "Ana"}],
    )
    created = rules.classify_events(db)
    assert len(created) == 1
    todo = db.get(Todo, created[0])
    assert todo.title.startswith("Meeting: Grading review")
    assert todo.due_date is not None       # drives the reminder
    assert todo.reason == "schedule:you-are-a-participant"


def test_meeting_you_are_not_invited_to_is_ignored(db):
    identity(db, user_id=99)
    make_event(
        db, etype="schedule", chat_id=None, basecamp_id=6, body="",
        title="Someone else's standup",
        participants=[{"id": 2, "name": "Ana"}],
    )
    assert rules.classify_events(db) == []


def test_a_document_naming_you_is_flagged(db):
    identity(db, user_id=99, name="Sam Reyes")
    make_event(db, etype="document", chat_id=None, basecamp_id=7,
               title="Shot list", body="Sam Reyes should sign this off")
    created = rules.classify_events(db)
    assert len(created) == 1
    assert db.get(Todo, created[0]).reason == "mention:by-name"


def test_an_unremarkable_upload_is_ignored(db):
    """Most of what lands in Docs & Files is FYI; the bar has to be higher than
    "a file appeared"."""
    identity(db, user_id=99, name="Sam Reyes")
    make_event(db, etype="upload", chat_id=None, basecamp_id=8,
               title="IMG_4821.jpg", body="")
    assert rules.classify_events(db) == []


def test_your_own_document_is_never_flagged(db):
    identity(db, user_id=99, name="Sam Reyes")
    make_event(db, etype="document", chat_id=None, basecamp_id=9, who_id=99,
               who="Sam Reyes", title="Notes", body="please review the budget")
    assert rules.classify_events(db) == []


# ── learning from confirm/dismiss history ────────────────────────────────────
def test_no_history_means_no_extra_prompt(db):
    assert ollama.feedback_examples(db) == ""


def test_kept_and_rejected_titles_both_appear(db):
    make_todo(db, title="Send the grading notes", status="confirmed")
    make_todo(db, title="Someone said good morning", status="dismissed")
    text = ollama.feedback_examples(db)
    assert "Send the grading notes" in text
    assert "Someone said good morning" in text
    assert "KEPT" in text and "REJECTED" in text


def test_manual_todos_are_excluded(db):
    """A to-do you typed yourself says nothing about where the line sits."""
    make_todo(db, title="Buy milk", status="confirmed", reason="manual")
    assert ollama.feedback_examples(db) == ""


def test_open_suggestions_are_not_yet_evidence(db):
    make_todo(db, title="Undecided thing", status="suggested")
    assert ollama.feedback_examples(db) == ""


def test_history_is_capped(db, monkeypatch):
    monkeypatch.setattr(ollama, "FEEDBACK_EXAMPLES", 3)
    for i in range(10):
        make_todo(db, title=f"Rejected {i}", status="dismissed")
    text = ollama.feedback_examples(db)
    assert text.count("Rejected") == 3


def test_feedback_is_appended_to_the_system_prompt(db):
    make_todo(db, title="Send the grading notes", status="confirmed")
    combined = ollama.build_system_prompt(db) + ollama.feedback_examples(db)
    assert "Send the grading notes" in combined
    assert combined.startswith("You are ")
