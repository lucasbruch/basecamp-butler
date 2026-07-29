"""Write-back gating.

Creating to-dos in a shared workspace on someone's behalf is the one thing here
that other people can see, so it's deliberately opt-in twice — a global setting
*and* a target list chosen for that specific project — and never happens twice
for the same suggestion.
"""
from app import runtime, writeback
from app.models import Project

from helpers import make_todo


def cfg(**over):
    return runtime.RuntimeConfig(**{**runtime.defaults(), "writeback_enabled": True, **over})


def project(db, *, todolist_id=55):
    db.add(Project(id=100, name="Feature Film", enabled=True, auto_add=False,
                   todolist_id=todolist_id, todolist_name="Post"))
    db.flush()


def test_eligible_when_enabled_and_targeted(db):
    project(db)
    todo = make_todo(db, status="confirmed", project_id=100)
    assert writeback.eligible(db, todo, cfg())


def test_not_eligible_when_globally_off(db):
    project(db)
    todo = make_todo(db, status="confirmed", project_id=100)
    assert not writeback.eligible(db, todo, cfg(writeback_enabled=False))


def test_not_eligible_without_a_target_list(db):
    """Global switch on but no list picked is not consent to post somewhere."""
    project(db, todolist_id=None)
    todo = make_todo(db, status="confirmed", project_id=100)
    assert not writeback.eligible(db, todo, cfg())


def test_not_eligible_for_an_unknown_project(db):
    todo = make_todo(db, status="confirmed", project_id=999)
    assert not writeback.eligible(db, todo, cfg())


def test_pings_are_never_written_back(db):
    """Pings live in Circles, which have no to-do lists to write into."""
    todo = make_todo(db, status="confirmed", project_id=None)
    assert not writeback.eligible(db, todo, cfg())


def test_never_pushed_twice(db):
    project(db)
    todo = make_todo(db, status="confirmed", project_id=100)
    todo.basecamp_todo_id = 12345
    db.flush()
    assert not writeback.eligible(db, todo, cfg())
