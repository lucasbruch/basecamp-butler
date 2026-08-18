"""Push confirmed suggestions back into Basecamp as real to-dos.

Until now the butler was strictly read-only: pressing ✅ flipped a row in the
local database and nothing else. That's fine for triage, but the to-do lives in
the one place your colleagues never look. With write-back on, confirming a
suggestion also creates the actual Basecamp to-do in a list you nominate per
project, so the work shows up where the team already works.

Deliberate constraints:

  * Opt-in twice — the global `writeback_enabled` setting *and* a target list
    chosen for that specific project. No target, no write. A tool that silently
    posts into a shared workspace on your behalf is not a good surprise.
  * At most once per to-do, guarded by `basecamp_todo_id`.
  * Never fatal. A failed write leaves the local to-do confirmed and records the
    reason in the activity feed; the button the user pressed still worked.

Writes use the same OAuth token as polling, so they act as you and can only
touch what you can already reach.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import activity, runtime
from .basecamp.client import BasecampClient, client_for
from .db import session_scope
from .models import Project, Todo
from .util import due_on, safe_url

log = logging.getLogger(__name__)


def _client(db: Session) -> BasecampClient | None:
    """A ready client, or None when Basecamp isn't connected yet."""
    return client_for(db)


def list_todolists(project_id: int) -> list[dict]:
    """Target lists to choose from on the Settings page. [] if unavailable."""
    with session_scope() as db:
        client = _client(db)
        if client is None:
            return []
        try:
            return [
                {"id": tl.get("id"), "name": tl.get("name") or "(untitled)"}
                for tl in client.todolists(project_id)
                if tl.get("id")
            ]
        except Exception:
            log.exception("Could not list to-do lists for project %s", project_id)
            return []
        finally:
            client.close()


def eligible(db: Session, todo: Todo, cfg: runtime.RuntimeConfig) -> bool:
    """Whether this to-do should be pushed to Basecamp right now."""
    if not cfg.writeback_enabled:
        return False
    if todo.basecamp_todo_id:  # already pushed
        return False
    if todo.project_id is None:  # Pings live in Circles, which have no to-do lists
        return False
    project = db.get(Project, todo.project_id)
    return bool(project and project.todolist_id)


def push(todo_id: int) -> bool:
    """Create the Basecamp to-do for `todo_id`. Returns whether it landed.

    Opens its own session: callers do this right after committing a status
    change, and the network round-trip has no business inside that transaction.
    """
    with session_scope() as db:
        cfg = runtime.load(db)
        todo = db.get(Todo, todo_id)
        if todo is None or not eligible(db, todo, cfg):
            return False
        project = db.get(Project, todo.project_id)
        target_list = project.todolist_id
        target_name = project.todolist_name or str(target_list)
        title = todo.title
        description = todo.notes
        # Basecamp wants a bare date; our due_date is a timestamp. An all-day
        # value round-trips unchanged, and a meeting's start collapses to the day
        # it falls on locally — the same day the dashboard shows.
        day = due_on(todo.due_date, cfg.tz, all_day=todo.due_all_day)
        due = day.strftime("%Y-%m-%d") if day else None

        client = _client(db)
        if client is None:
            return False
        try:
            created = client.create_todo(
                todo.project_id,
                target_list,
                title,
                description=description,
                due_on=due,
            )
        except Exception as exc:
            log.exception("Write-back failed for to-do %s", todo_id)
            activity.record(
                db,
                "error",
                f"Couldn't add “{title[:80]}” to Basecamp — {type(exc).__name__}. "
                "It's still confirmed here.",
            )
            return False
        finally:
            client.close()

        todo.basecamp_todo_id = created.get("id")
        todo.basecamp_url = safe_url(created.get("app_url") or created.get("url"))
        activity.record(
            db,
            "writeback",
            f"Added “{title[:80]}” to Basecamp ({target_name}).",
            url=todo.basecamp_url,
        )
        return True
