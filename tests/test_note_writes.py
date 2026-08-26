"""A pass that decided nothing new writes nothing new.

`_note` and `_note_pass` are rewritten by every pass — once a minute, and for
`_note` once per thread that has had a line in the last LOOKBACK_HOURS. That is
a full day of per-minute writes after anyone pings, all of them re-recording
sentences that already said the same thing.

Skipping the unchanged ones is only safe because of the refresh floor, which is
what these tests are mostly about: `decisions` prunes on the `at` stamp, so a
reason that never rewrote would quietly delete the row explaining a live
conversation. The floor is a floor, not a skip.
"""
import json
from datetime import timedelta

from sqlalchemy import event

from app import autoreply
from app.models import AppState
from app.util import as_aware, parse_bc_datetime

KEY = f"{autoreply.WHY_PREFIX}7"


def stamp(db, key=KEY):
    row = db.get(AppState, key)
    return json.loads(row.value)["at"] if row and row.value else None


def store(db, at, why="In the cooldown window.", person="Ana", key=KEY):
    """Put a note in the past, so a floor can be crossed without waiting."""
    db.merge(AppState(key=key, value=json.dumps(
        {"person": person, "why": why, "at": at.isoformat()}
    )))
    db.flush()


def count_state_writes(db):
    seen = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        head = " ".join(statement.split())[:60].upper()
        if head.startswith(("UPDATE APP_STATE", "INSERT INTO APP_STATE")):
            seen.append(head)

    return seen


# ── per-conversation notes ──────────────────────────────────────────────────
def test_an_unchanged_reason_is_not_rewritten(db):
    autoreply._note(db, 7, "Ana", "In the cooldown window.")
    first = stamp(db)

    writes = count_state_writes(db)
    autoreply._note(db, 7, "Ana", "In the cooldown window.")

    assert stamp(db) == first
    assert writes == []


def test_a_changed_reason_is_written_at_once(db):
    """A stale explanation on /replies is the thing this page exists to avoid."""
    autoreply._note(db, 7, "Ana", "In the cooldown window.")
    autoreply._note(db, 7, "Ana", "Replied.")
    assert autoreply.decisions(db)[0]["why"] == "Replied."


def test_a_changed_person_is_written_too(db):
    """The comparison is the whole note, not just its sentence."""
    autoreply._note(db, 7, "Ana", "In the cooldown window.")
    autoreply._note(db, 7, "Bo", "In the cooldown window.")
    assert autoreply.decisions(db)[0]["person"] == "Bo"


def test_a_stale_stamp_is_refreshed_though_the_reason_stands(db):
    """Past the floor the note is rewritten even with nothing new to say."""
    old = autoreply.utcnow() - autoreply.NOTE_REFRESH - timedelta(minutes=1)
    store(db, old)

    autoreply._note(db, 7, "Ana", "In the cooldown window.")

    assert stamp(db) != old.isoformat()


def test_a_steady_reason_keeps_its_row_off_the_pruner(db):
    """The consequence the floor exists for.

    A conversation can sit on one reason — in a cooldown, nothing needing an
    answer — for longer than WHY_KEEP_DAYS. Were an unchanged reason simply
    never written, `decisions` would prune the row while the conversation was
    still live, and /replies would stop explaining the very case it is for.
    """
    nearly_stale = autoreply.utcnow() - timedelta(days=autoreply.WHY_KEEP_DAYS - 1)
    store(db, nearly_stale)

    autoreply._note(db, 7, "Ana", "In the cooldown window.")

    refreshed = as_aware(parse_bc_datetime(stamp(db)))
    assert autoreply.utcnow() - refreshed < timedelta(minutes=1)
    assert [d["chat_id"] for d in autoreply.decisions(db)] == ["7"]


def test_a_hand_edited_row_is_replaced_rather_than_trusted(db):
    db.merge(AppState(key=KEY, value="not json"))
    db.flush()

    autoreply._note(db, 7, "Ana", "In the cooldown window.")

    assert autoreply.decisions(db)[0]["why"] == "In the cooldown window."


# ── the pass summary ────────────────────────────────────────────────────────
def test_an_unchanged_pass_summary_is_not_rewritten(db):
    autoreply._note_pass(db, "No Ping messages arrived.")
    first = stamp(db, autoreply.LAST_PASS_KEY)

    writes = count_state_writes(db)
    autoreply._note_pass(db, "No Ping messages arrived.")

    assert stamp(db, autoreply.LAST_PASS_KEY) == first
    assert writes == []


def test_a_pass_summary_refreshes_past_its_floor(db):
    old = autoreply.utcnow() - autoreply.PASS_REFRESH - timedelta(minutes=1)
    db.merge(AppState(key=autoreply.LAST_PASS_KEY, value=json.dumps(
        {"why": "No Ping messages arrived.", "threads": 0, "composed": 0,
         "at": old.isoformat()}
    )))
    db.flush()

    autoreply._note_pass(db, "No Ping messages arrived.")

    assert stamp(db, autoreply.LAST_PASS_KEY) != old.isoformat()


def test_a_pass_that_did_something_is_written_though_the_words_match(db):
    """Same sentence, different counts, is still news."""
    autoreply._note_pass(db, "Looked at Pings.", threads=1, composed=0)
    autoreply._note_pass(db, "Looked at Pings.", threads=1, composed=1)
    assert autoreply.last_pass(db)["composed"] == 1


def test_an_hour_of_quiet_passes_costs_one_write(db):
    """The point of the whole change, stated as a number.

    Sixty passes an hour apart used to be sixty row versions. The floor makes
    the unchanged fifty-nine free, and the pruner-safety tests above are what
    say the surviving one is enough.
    """
    autoreply._note(db, 7, "Ana", "In the cooldown window.")

    writes = count_state_writes(db)
    for _ in range(59):
        autoreply._note(db, 7, "Ana", "In the cooldown window.")

    assert writes == []
