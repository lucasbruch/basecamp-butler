"""FastAPI web UI: review/confirm/dismiss to-dos, tweak settings, read reports."""
from __future__ import annotations

import base64
import hmac
import html
import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from .. import runtime, todos as todo_actions, writeback
from ..basecamp.auth import (
    build_authorize_url,
    consume_state,
    discover_account,
    exchange_code,
    store_token,
)
from ..config import settings
from ..db import session_scope
from ..models import ActivityLog, AppState, MutedSender, Project, Report, Todo
from ..util import as_aware, due_on, parse_bc_datetime, utcnow

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ("suggested", "confirmed", "dismissed", "done")
ACTIVITY_KINDS = ("poll", "ping", "campfire", "llm", "rule", "notify", "writeback", "error")


def _as_aware(value):
    """Coerce a template value to a tz-aware datetime, or None.

    Subtracting a naive from an aware datetime raises, which would turn one odd
    row into a 500 on the dashboard, so normalise rather than trust."""
    if value is None:
        return None
    dt = parse_bc_datetime(value) if isinstance(value, str) else value
    return as_aware(dt)


def _timeago(value) -> str:
    """Render a datetime (or ISO string) as a compact relative time."""
    dt = _as_aware(value)
    if dt is None:
        return "never"
    secs = int((utcnow() - dt).total_seconds())
    if secs < 0:
        # A future time (a snooze that hasn't fired) reads as "in 2h", not "0m ago".
        secs = -secs
        if secs < 60:
            return "in a moment"
        if secs < 3600:
            return f"in {secs // 60}m"
        if secs < 86400:
            return f"in {secs // 3600}h"
        return f"in {secs // 86400}d"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _localtime(value, tz_name: str = "UTC", fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a UTC datetime in the configured local zone.

    Everything is stored in UTC; showing it that way made every timestamp in the
    UI subtly wrong for anyone not on UTC — including due dates, which is the
    one place being off by an hour actually matters."""
    dt = _as_aware(value)
    if dt is None:
        return "—"
    return dt.astimezone(runtime.resolve_tz(tz_name)).strftime(fmt)


def _duedate(todo, tz_name: str = "UTC") -> str:
    """Format a to-do's due date as the calendar day the user would name.

    Takes the whole row rather than the timestamp because the answer depends on
    `due_all_day` — see `util.due_on`."""
    day = due_on(
        _as_aware(getattr(todo, "due_date", None)),
        runtime.resolve_tz(tz_name),
        all_day=bool(getattr(todo, "due_all_day", False)),
    )
    return day.strftime("%Y-%m-%d") if day else "—"


TEMPLATES.env.filters["timeago"] = _timeago
TEMPLATES.env.filters["localtime"] = _localtime
TEMPLATES.env.filters["duedate"] = _duedate


def _dashboard_status(db, cfg: runtime.RuntimeConfig) -> dict:
    """Read the small heartbeat keys the poller/classifier stamp into app_state."""
    def g(key: str) -> str | None:
        row = db.get(AppState, key)
        return row.value if row else None

    return {
        "last_poll_at": parse_bc_datetime(g("last_poll_at")),
        "last_poll_new": g("last_poll_new"),
        "last_poll_ok": g("last_poll_ok"),
        "last_poll_error": g("last_poll_error"),
        "pings_checked_at": parse_bc_datetime(g("pings_checked_at")),
        "pings_visible": g("pings_visible"),
        "llm_status": g("llm_status"),
        "llm_checked_at": parse_bc_datetime(g("llm_checked_at")),
        "classifier": cfg.classifier,
        "poll_pings": settings.poll_pings,
        "quiet_now": cfg.is_quiet_now(),
        "writeback": cfg.writeback_enabled,
    }


def _token_ok(candidate: str | None) -> bool:
    """Constant-time compare of a presented secret against the configured one."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate, settings.web_auth_token)


def _request_authorized(request: Request) -> bool:
    """True if the request carries the shared secret via Bearer or Basic auth.

    The token is intentionally NOT accepted from the query string: a `?token=`
    would leak the secret into access logs, browser history, and Referer headers
    (and we redirect to Referer after actions). Browsers use the Basic prompt;
    the ntfy action buttons send it as a Bearer header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _token_ok(auth[7:].strip())
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return False
        # Browsers send "user:password"; the password carries the token.
        _, _, password = decoded.partition(":")
        return _token_ok(password)
    return False


def _same_origin(request: Request) -> bool:
    """Reject state-changing requests initiated by another site.

    Every mutating route here is a plain POST, and HTTP Basic credentials ride
    along on cross-origin form submissions — so without this check a page on any
    other site could dismiss your to-dos or rewrite your settings while you were
    logged in.

    Modern browsers send `Origin` on every POST, so a mismatch is a genuine
    cross-site attempt. A *missing* Origin means a non-browser client (the ntfy
    action buttons, curl), which has no ambient credentials to abuse and must
    present the Bearer token instead.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc == host


def _safe_redirect(request: Request, fallback: str = "/") -> str:
    """A same-site path to bounce back to after a form action.

    `Referer` is set by the browser but is influenced by whoever linked to us, so
    returning it verbatim made this an open redirect. Only the path+query of a
    same-host referer survives."""
    ref = request.headers.get("Referer") or ""
    if not ref:
        return fallback
    try:
        parts = urlsplit(ref)
    except ValueError:
        return fallback
    if parts.netloc and parts.netloc != request.headers.get("Host", ""):
        return fallback
    dest = parts.path or fallback
    if parts.query:
        dest = f"{dest}?{parts.query}"
    # "//evil.com" is a protocol-relative URL, not a local path.
    if not dest.startswith("/") or dest.startswith("//"):
        return fallback
    return dest


def _maybe_writeback(todo_id: int, action: str) -> None:
    """Create the real Basecamp to-do when a suggestion is confirmed."""
    if action != "confirm":
        return
    try:
        writeback.push(todo_id)
    except Exception:
        log.exception("Write-back failed for todo %s", todo_id)


def create_app() -> FastAPI:
    app = FastAPI(title="Basecamp Butler")

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        # No token configured → open (LAN-only default). /healthz is always open
        # so container/orchestrator health checks don't need the secret.
        if settings.web_auth_token and request.url.path != "/healthz":
            if not _request_authorized(request):
                return Response(
                    "Authentication required.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Basecamp Butler"'},
                )
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin(request):
            log.warning(
                "Blocked cross-origin %s %s from %s",
                request.method,
                request.url.path,
                request.headers.get("Origin"),
            )
            return Response("Cross-origin request refused.", status_code=403)
        return await call_next(request)

    # ── dashboard ────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with session_scope() as db:
            cfg = runtime.load(db)
            now = utcnow()
            active = (
                db.execute(
                    select(Todo)
                    .where(Todo.status.in_(("suggested", "confirmed")))
                    .order_by(Todo.created_at.desc())
                )
                .scalars()
                .all()
            )
            # A snoozed to-do is deliberately out of sight until it's due back.
            awake = [t for t in active if not t.snoozed_until or t.snoozed_until <= now]
            snoozed = [t for t in active if t.snoozed_until and t.snoozed_until > now]
            suggested = [t for t in awake if t.status == "suggested"]
            confirmed = [t for t in awake if t.status == "confirmed"]
            return TEMPLATES.TemplateResponse(
                request,
                "index.html",
                {
                    "suggested": suggested,
                    "confirmed": confirmed,
                    "snoozed": snoozed,
                    "projects": _project_names(db),
                    "status": _dashboard_status(db, cfg),
                    "tz": cfg.timezone,
                    "snooze_actions": todo_actions.SNOOZE_ACTIONS,
                    "suggest_max": max((t.id for t in suggested), default=0),
                },
            )

    @app.get("/api/badge")
    def badge():
        # Tiny poll target for the favicon badge and the change-detecting
        # refresh: ids autoincrement, so the page can watermark "seen up to N"
        # and light the badge when something higher appears. `stamp` changes
        # whenever any to-do changes, which is what lets the page decide to
        # reload instead of reloading blindly on a timer.
        with session_scope() as db:
            now = utcnow()
            count, max_id = db.execute(
                select(func.count(Todo.id), func.max(Todo.id)).where(
                    Todo.status == "suggested",
                    or_(Todo.snoozed_until.is_(None), Todo.snoozed_until <= now),
                )
            ).one()
            latest_change = db.execute(select(func.max(Todo.updated_at))).scalar()
            return {
                "suggested": count,
                "max_id": max_id or 0,
                "stamp": latest_change.isoformat() if latest_change else "",
            }

    # ── to-dos ───────────────────────────────────────────────────────────────
    @app.get("/todos", response_class=HTMLResponse)
    def todos(
        request: Request,
        status: str | None = None,
        project: int | None = None,
        q: str | None = None,
    ):
        with session_scope() as db:
            cfg = runtime.load(db)
            stmt = select(Todo).order_by(Todo.created_at.desc())
            if status in STATUSES:
                stmt = stmt.where(Todo.status == status)
            if project:
                stmt = stmt.where(Todo.project_id == project)
            term = (q or "").strip()
            if term:
                like = f"%{term}%"
                stmt = stmt.where(
                    or_(Todo.title.ilike(like), Todo.notes.ilike(like))
                )
            items = db.execute(stmt.limit(500)).scalars().all()
            return TEMPLATES.TemplateResponse(
                request,
                "todos.html",
                {
                    "items": items,
                    "projects": _project_names(db),
                    "all_projects": db.execute(
                        select(Project).order_by(Project.name)
                    ).scalars().all(),
                    "statuses": STATUSES,
                    "active_status": status,
                    "active_project": project,
                    "query": term,
                    "tz": cfg.timezone,
                    "snooze_actions": todo_actions.SNOOZE_ACTIONS,
                },
            )

    @app.post("/todos/{todo_id}/{action}")
    def todo_action(todo_id: int, action: str, request: Request):
        with session_scope() as db:
            cfg = runtime.load(db)
            todo_actions.apply_action(db, todo_id, action, cfg)
        _maybe_writeback(todo_id, action)
        # Return the user to the page they acted from (e.g. a filtered /todos
        # view) — validated, so a hostile Referer can't turn this into a redirect
        # to another site.
        return RedirectResponse(_safe_redirect(request), status_code=303)

    @app.post("/api/todos/{todo_id}/{action}")
    def api_todo_action(todo_id: int, action: str):
        """JSON endpoint for notification buttons (ntfy) and the inline UI."""
        if action not in todo_actions.ALL_ACTIONS:
            raise HTTPException(status_code=400, detail="unknown action")
        with session_scope() as db:
            cfg = runtime.load(db)
            todo = todo_actions.apply_action(db, todo_id, action, cfg)
            if todo is None:
                raise HTTPException(status_code=404, detail="todo not found")
            payload = {
                "ok": True,
                "id": todo_id,
                "status": todo.status,
                "snoozed_until": todo.snoozed_until.isoformat() if todo.snoozed_until else None,
            }
        _maybe_writeback(todo_id, action)
        return payload

    @app.post("/api/todos/bulk")
    def api_todos_bulk(ids: str = Form(...), action: str = Form(...)):
        """Apply one action to many to-dos — "dismiss everything from that
        Campfire flood" shouldn't be twenty clicks."""
        if action not in todo_actions.ALL_ACTIONS:
            raise HTTPException(status_code=400, detail="unknown action")
        wanted = [int(p) for p in ids.split(",") if p.strip().isdigit()]
        if not wanted:
            return {"ok": False, "error": "Nothing selected.", "changed": 0}
        with session_scope() as db:
            cfg = runtime.load(db)
            changed = sum(1 for i in wanted if todo_actions.apply_action(db, i, action, cfg))
        if action == "confirm":
            for todo_id in wanted:
                _maybe_writeback(todo_id, action)
        return {"ok": True, "changed": changed, "action": action}

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

    # ── settings ─────────────────────────────────────────────────────────────
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        from ..classifier import ollama

        with session_scope() as db:
            cfg = runtime.load(db)
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                {
                    "projects": db.execute(
                        select(Project).order_by(Project.name)
                    ).scalars().all(),
                    "settings": settings,
                    "cfg": cfg,
                    "env_defaults": runtime.defaults(),
                    "overridden": runtime.overrides(db),
                    "classifiers": runtime.CLASSIFIERS,
                    "channels": runtime.CHANNELS,
                    "telegram_enabled": settings.telegram_enabled,
                    "ntfy_enabled": settings.ntfy_enabled,
                    "authorized": _is_authorized(db),
                    "muted": db.execute(
                        select(MutedSender).order_by(MutedSender.name)
                    ).scalars().all(),
                    "tz": cfg.timezone,
                    "zone_groups": runtime.zone_groups(cfg.timezone),
                    "rejected": {
                        k for k in request.query_params.get("rejected", "").split(",")
                        if k in runtime.KEYS
                    },
                    "assistant": {
                        "role": _appstate(db, "llm_role") or "",
                        "topics": _appstate(db, "llm_topics") or "",
                        "override": _appstate(db, "llm_prompt_override") or "",
                        "default_role": ollama.DEFAULT_ROLE,
                        "default_topics": ollama.DEFAULT_TOPICS,
                        "active_prompt": ollama.build_system_prompt(db),
                        "feedback": ollama.feedback_examples(db),
                    },
                },
            )

    @app.post("/settings/runtime")
    async def update_runtime(request: Request):
        """Save the editable settings. Anything absent from the form is left
        alone, so a checkbox-only sub-form can't blank out the text fields."""
        form = await request.form()
        updates = {}
        for key in runtime.KEYS:
            if f"{key}__present" not in form:
                continue
            # An unchecked checkbox submits nothing; its companion marker tells
            # us the field was on screen, so absence genuinely means "off".
            updates[key] = form.get(key, "false" if key.endswith("_enabled") else "")
        rejected: set[str] = set()
        with session_scope() as db:
            runtime.save(db, updates, rejected)
        dest = _safe_redirect(request, "/settings")
        if rejected:
            # Only the key names travel in the URL, and the page matches them
            # against a known list — the value the user typed is never echoed.
            sep = "&" if "?" in dest else "?"
            dest = f"{dest}{sep}rejected={','.join(sorted(rejected))}"
        return RedirectResponse(dest, status_code=303)

    @app.post("/settings/assistant")
    def update_assistant(
        role: str = Form(""), topics: str = Form(""), prompt_override: str = Form("")
    ):
        with session_scope() as db:
            db.merge(AppState(key="llm_role", value=role.strip()))
            db.merge(AppState(key="llm_topics", value=topics.strip()))
            db.merge(AppState(key="llm_prompt_override", value=prompt_override.strip()))
        return RedirectResponse("/settings", status_code=303)

    @app.post("/api/assistant/test")
    def api_assistant_test(
        sample: str = Form(""),
        role: str = Form(""),
        topics: str = Form(""),
        prompt_override: str = Form(""),
    ):
        """Run one made-up message through the (possibly unsaved) persona."""
        from ..classifier import ollama

        if not sample.strip():
            return {"ok": False, "error": "Type a sample message to test."}
        return ollama.test_prompt(
            sample, role=role, topics=topics, override=prompt_override
        )

    @app.post("/settings/project/{project_id}")
    def update_project(
        project_id: int,
        auto_add: str = Form(""),
        enabled: str = Form(""),
        todolist_id: str = Form(""),
    ):
        with session_scope() as db:
            proj = db.get(Project, project_id)
            if proj:
                proj.auto_add = auto_add == "on"
                proj.enabled = enabled == "on"
                if todolist_id.isdigit():
                    proj.todolist_id = int(todolist_id)
                    # Remember the name so the UI can show it without a round trip.
                    for tl in _cached_todolists(project_id):
                        if str(tl["id"]) == todolist_id:
                            proj.todolist_name = tl["name"]
                            break
                elif todolist_id == "":
                    proj.todolist_id = None
                    proj.todolist_name = None
        return RedirectResponse("/settings", status_code=303)

    @app.get("/api/projects/{project_id}/todolists")
    def api_todolists(project_id: int):
        """Target lists for write-back, fetched on demand from Basecamp."""
        return {"lists": _cached_todolists(project_id)}

    @app.post("/settings/mute")
    def add_mute(request: Request, name: str = Form("")):
        clean = name.strip()[:200]
        if clean:
            with session_scope() as db:
                exists = db.execute(
                    select(MutedSender).where(func.lower(MutedSender.name) == clean.lower())
                ).scalar_one_or_none()
                if exists is None:
                    db.add(MutedSender(name=clean))
        return RedirectResponse(_safe_redirect(request, "/settings"), status_code=303)

    @app.post("/settings/mute/{mute_id}/delete")
    def remove_mute(mute_id: int, request: Request):
        with session_scope() as db:
            row = db.get(MutedSender, mute_id)
            if row is not None:
                db.delete(row)
        return RedirectResponse(_safe_redirect(request, "/settings"), status_code=303)

    # ── oauth ────────────────────────────────────────────────────────────────
    @app.get("/oauth/start")
    def oauth_start():
        """Kick off the OAuth handshake from a browser (ideal for headless NAS)."""
        with session_scope() as db:
            url = build_authorize_url(db)
        return RedirectResponse(url, status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(
        code: str | None = None, error: str | None = None, state: str | None = None
    ):
        """Redirect target for the OAuth handshake (usable from the running app)."""
        if error or not code:
            return HTMLResponse(
                f"<h1>Authorization failed</h1><p>{html.escape(error or 'no code')}</p>", 400
            )
        with session_scope() as db:
            if not consume_state(db, state):
                log.warning("OAuth callback with a bad state parameter — refusing.")
                return HTMLResponse(
                    "<h1>Authorization failed</h1><p>That sign-in link didn't come "
                    "from this app. Start again from Settings.</p>",
                    400,
                )
        token_data = exchange_code(code)
        account_id, api_href = discover_account(token_data["access_token"])
        with session_scope() as db:
            store_token(db, token_data, account_id=account_id, api_href=api_href)
        return HTMLResponse(
            "<h1>✅ Basecamp connected</h1>"
            f"<p>Account {account_id}. You can close this tab — polling will begin shortly.</p>"
            '<p><a href="/">Go to dashboard</a></p>'
        )

    # ── reports ──────────────────────────────────────────────────────────────
    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request):
        from .. import report

        with session_scope() as db:
            cfg = runtime.load(db)
            history = (
                db.execute(select(Report).order_by(Report.created_at.desc()).limit(30))
                .scalars()
                .all()
            )
            return TEMPLATES.TemplateResponse(
                request,
                "report.html",
                {
                    "min_hours": report.MIN_HOURS,
                    "max_hours": report.MAX_HOURS,
                    "default_hours": cfg.daily_report_hours,
                    "push_enabled": settings.ntfy_enabled or settings.telegram_enabled,
                    "notify_channel": cfg.notify_channel,
                    "history": history,
                    "cfg": cfg,
                    "tz": cfg.timezone,
                },
            )

    @app.post("/api/report")
    def api_report(hours: str = Form(...)):
        """Generate a condensed report of the last N hours. Returns display JSON."""
        from .. import report

        with session_scope() as db:
            result = report.generate_report(db, hours)
            result["stored_id"] = report.store(db, result)
            return result

    @app.post("/api/report/push")
    def api_report_push(body: str = Form(...), hours: str = Form("")):
        """Send an already-generated report to the user's phone via the notifier."""
        from .. import notifier, report

        text = body.strip()
        if not text:
            return {"ok": False, "error": "Nothing to send — generate a report first."}
        window = report.humanize_hours(report.clamp_hours(hours)) if hours else ""
        title = f"🫖 Basecamp report · last {window}" if window else "🫖 Basecamp report"
        if not notifier.notify_text(title, text[:3800]):
            return {"ok": False, "error": "No push channel is configured."}
        return {"ok": True}

    @app.get("/api/report/{report_id}")
    def api_report_get(report_id: int):
        with session_scope() as db:
            row = db.get(Report, report_id)
            if row is None:
                raise HTTPException(status_code=404, detail="report not found")
            return {
                "ok": True,
                "id": row.id,
                "report": row.body,
                "hours": row.hours,
                "source": row.source,
                "model": row.model,
                "event_count": row.event_count,
                "todo_count": row.todo_count,
                "scheduled": row.scheduled,
                "created_at": row.created_at.isoformat(),
            }

    # ── activity ─────────────────────────────────────────────────────────────
    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, kind: str | None = None, q: str | None = None):
        with session_scope() as db:
            cfg = runtime.load(db)
            stmt = select(ActivityLog).order_by(ActivityLog.created_at.desc())
            if kind in ACTIVITY_KINDS:
                stmt = stmt.where(ActivityLog.kind == kind)
            term = (q or "").strip()
            if term:
                like = f"%{term}%"
                stmt = stmt.where(
                    or_(ActivityLog.summary.ilike(like), ActivityLog.detail.ilike(like))
                )
            entries = db.execute(stmt.limit(300)).scalars().all()
            return TEMPLATES.TemplateResponse(
                request,
                "activity.html",
                {
                    "entries": entries,
                    "kinds": ACTIVITY_KINDS,
                    "active_kind": kind if kind in ACTIVITY_KINDS else None,
                    "query": term,
                    "tz": cfg.timezone,
                },
            )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": utcnow().isoformat()}

    return app


# Basecamp's to-do lists change rarely and the lookup is three API calls, so a
# tiny process-local cache keeps the Settings page snappy. Cleared on restart,
# which is often enough for something the user edits by hand.
_TODOLIST_CACHE: dict[int, list[dict]] = {}


def _cached_todolists(project_id: int) -> list[dict]:
    if project_id not in _TODOLIST_CACHE:
        _TODOLIST_CACHE[project_id] = writeback.list_todolists(project_id)
    return _TODOLIST_CACHE[project_id]


def _project_names(db) -> dict[int, str]:
    return {p.id: p.name for p in db.execute(select(Project)).scalars()}


def _appstate(db, key: str) -> str | None:
    row = db.get(AppState, key)
    return row.value if row else None


def _is_authorized(db) -> bool:
    from ..models import OAuthToken

    return db.get(OAuthToken, 1) is not None
