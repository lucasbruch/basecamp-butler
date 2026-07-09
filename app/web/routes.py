"""FastAPI web UI: review/confirm/dismiss to-dos, tweak per-project settings."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..basecamp.auth import (
    build_authorize_url,
    discover_account,
    exchange_code,
    store_token,
)
from ..config import settings
from ..db import session_scope
from ..models import Project, Todo
from ..util import utcnow

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ("suggested", "confirmed", "dismissed", "done")


def create_app() -> FastAPI:
    app = FastAPI(title="Basecamp Butler")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with session_scope() as db:
            active = (
                db.execute(
                    select(Todo)
                    .where(Todo.status.in_(("suggested", "confirmed")))
                    .order_by(Todo.created_at.desc())
                )
                .scalars()
                .all()
            )
            suggested = [t for t in active if t.status == "suggested"]
            confirmed = [t for t in active if t.status == "confirmed"]
            projects = _project_names(db)
            return TEMPLATES.TemplateResponse(
                request,
                "index.html",
                {
                    "suggested": suggested,
                    "confirmed": confirmed,
                    "projects": projects,
                },
            )

    @app.get("/todos", response_class=HTMLResponse)
    def todos(request: Request, status: str | None = None, project: int | None = None):
        with session_scope() as db:
            stmt = select(Todo).order_by(Todo.created_at.desc())
            if status in STATUSES:
                stmt = stmt.where(Todo.status == status)
            if project:
                stmt = stmt.where(Todo.project_id == project)
            items = db.execute(stmt).scalars().all()
            projects = _project_names(db)
            all_projects = db.execute(select(Project).order_by(Project.name)).scalars().all()
            return TEMPLATES.TemplateResponse(
                request,
                "todos.html",
                {
                    "items": items,
                    "projects": projects,
                    "all_projects": all_projects,
                    "statuses": STATUSES,
                    "active_status": status,
                    "active_project": project,
                },
            )

    @app.post("/todos/{todo_id}/{action}")
    def todo_action(todo_id: int, action: str):
        mapping = {
            "confirm": "confirmed",
            "dismiss": "dismissed",
            "done": "done",
            "reopen": "suggested",
        }
        new_status = mapping.get(action)
        if new_status:
            with session_scope() as db:
                todo = db.get(Todo, todo_id)
                if todo:
                    todo.status = new_status
        return RedirectResponse("/", status_code=303)

    @app.post("/api/todos/{todo_id}/{action}")
    def api_todo_action(todo_id: int, action: str):
        """JSON endpoint for notification action buttons (ntfy). Returns 200."""
        mapping = {
            "confirm": "confirmed",
            "dismiss": "dismissed",
            "done": "done",
            "reopen": "suggested",
        }
        new_status = mapping.get(action)
        if not new_status:
            raise HTTPException(status_code=400, detail="unknown action")
        with session_scope() as db:
            todo = db.get(Todo, todo_id)
            if todo is None:
                raise HTTPException(status_code=404, detail="todo not found")
            todo.status = new_status
        return {"ok": True, "id": todo_id, "status": new_status}

    @app.post("/todos")
    def add_todo(title: str = Form(...), notes: str = Form(""), project_id: str = Form("")):
        with session_scope() as db:
            db.add(
                Todo(
                    title=title.strip()[:1000],
                    notes=notes.strip() or None,
                    project_id=int(project_id) if project_id.isdigit() else None,
                    status="confirmed",
                    reason="manual",
                )
            )
        return RedirectResponse("/todos", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with session_scope() as db:
            projects = db.execute(select(Project).order_by(Project.name)).scalars().all()
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                {
                    "projects": projects,
                    "settings": settings,
                    "telegram_enabled": settings.telegram_enabled,
                    "ntfy_enabled": settings.ntfy_enabled,
                    "authorized": _is_authorized(db),
                },
            )

    @app.post("/settings/project/{project_id}")
    def update_project(project_id: int, auto_add: str = Form(""), enabled: str = Form("")):
        with session_scope() as db:
            proj = db.get(Project, project_id)
            if proj:
                proj.auto_add = auto_add == "on"
                proj.enabled = enabled == "on"
        return RedirectResponse("/settings", status_code=303)

    @app.get("/oauth/start")
    def oauth_start():
        """Kick off the OAuth handshake from a browser (ideal for headless NAS)."""
        return RedirectResponse(build_authorize_url(), status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(code: str | None = None, error: str | None = None):
        """Redirect target for the OAuth handshake (usable from the running app)."""
        if error or not code:
            return HTMLResponse(f"<h1>Authorization failed</h1><p>{error or 'no code'}</p>", 400)
        token_data = exchange_code(code)
        account_id, api_href = discover_account(token_data["access_token"])
        with session_scope() as db:
            store_token(db, token_data, account_id=account_id, api_href=api_href)
        return HTMLResponse(
            "<h1>✅ Basecamp connected</h1>"
            f"<p>Account {account_id}. You can close this tab — polling will begin shortly.</p>"
            '<p><a href="/">Go to dashboard</a></p>'
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": utcnow().isoformat()}

    return app


def _project_names(db) -> dict[int, str]:
    return {p.id: p.name for p in db.execute(select(Project)).scalars()}


def _is_authorized(db) -> bool:
    from ..models import OAuthToken

    return db.get(OAuthToken, 1) is not None
