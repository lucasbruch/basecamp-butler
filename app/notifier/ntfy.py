"""ntfy notifier: push to ntfy.sh (or a self-hosted server) with action buttons.

Free, no account, no bot. Subscribe to your topic in the ntfy phone/desktop app
and messages arrive instantly. Action buttons POST back to this app's /api routes
so you can Add / Dismiss / mark Done straight from the notification.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from ..db import session_scope
from ..models import Todo

log = logging.getLogger(__name__)


def _publish(payload: dict) -> None:
    if not settings.ntfy_enabled:
        return
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    body = {**payload, "topic": settings.ntfy_topic}
    try:
        # ntfy "JSON publishing": POST to the server root with the topic in the body.
        resp = httpx.post(
            settings.ntfy_server.rstrip("/") + "/",
            json=body,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("ntfy publish failed")


def _build_actions(todo: Todo, *, confirmed: bool) -> list[dict]:
    """Up to 3 action buttons (ntfy's max): primary, dismiss, open.

    The http actions call this app back, so they need app_base_url set and the
    device to be able to reach the NAS (same LAN / VPN).
    """
    base = settings.app_base_url.rstrip("/")
    # When the app is secured with WEB_AUTH_TOKEN, the callback buttons must carry
    # it or they'd get a 401. ntfy http actions support custom headers.
    headers = (
        {"Authorization": f"Bearer {settings.web_auth_token}"}
        if settings.web_auth_token
        else {}
    )
    actions: list[dict] = []
    if base:
        action, label = ("done", "✔ Done") if confirmed else ("confirm", "✅ Add")
        actions.append({
            "action": "http", "label": label,
            "url": f"{base}/api/todos/{todo.id}/{action}",
            "method": "POST", "clear": True, "headers": headers,
        })
        actions.append({
            "action": "http", "label": "✖ Dismiss",
            "url": f"{base}/api/todos/{todo.id}/dismiss",
            "method": "POST", "clear": True, "headers": headers,
        })
    view_url = todo.source_url or (base or None)
    if view_url:
        actions.append({"action": "view", "label": "Open", "url": view_url})
    return actions


def send_text(title: str, message: str) -> None:
    """Push a plain-text notification (e.g. an on-demand report) to the topic."""
    if not settings.ntfy_enabled:
        return
    _publish({"title": title, "message": message, "tags": ["scroll"], "priority": 3})


def notify_new_todo(todo_id: int) -> None:
    if not settings.ntfy_enabled:
        return
    with session_scope() as db:
        todo = db.get(Todo, todo_id)
        if todo is None or todo.status not in ("suggested", "confirmed"):
            return
        confirmed = todo.status == "confirmed"
        title = "🎬 New to-do" if confirmed else "🎬 Suggestion"
        lines = [todo.title]
        if todo.reason:
            lines.append(f"· {todo.reason}")
        if todo.due_date:
            lines.append(f"📅 due {todo.due_date:%Y-%m-%d}")
        payload = {
            "title": title,
            "message": "\n".join(lines),
            "tags": ["memo"] if confirmed else ["bulb"],
            "priority": 3,
            "actions": _build_actions(todo, confirmed=confirmed),
        }
    _publish(payload)


def notify_reminder(todo_id: int) -> None:
    if not settings.ntfy_enabled:
        return
    with session_scope() as db:
        todo = db.get(Todo, todo_id)
        if todo is None:
            return
        lines = [todo.title]
        if todo.due_date:
            lines.append(f"📅 due {todo.due_date:%Y-%m-%d}")
        payload = {
            "title": "⏰ Reminder",
            "message": "\n".join(lines),
            "tags": ["alarm_clock"],
            "priority": 4,
            "actions": _build_actions(todo, confirmed=True),
        }
    _publish(payload)
