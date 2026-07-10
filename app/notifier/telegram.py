"""Telegram notifier: push suggestions/reminders + handle inline-button replies.

Free, no inbound server required — outbound HTTPS for sending, and long-polling
getUpdates for the button callbacks (so no public webhook/URL is needed).
"""
from __future__ import annotations

import html
import logging
import threading
import time

import httpx

from ..config import settings
from ..db import session_scope
from ..models import Todo

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def _api_url(method: str) -> str:
    return f"{API}/bot{settings.telegram_bot_token}/{method}"


def _post(method: str, payload: dict) -> dict | None:
    if not settings.telegram_enabled:
        return None
    try:
        resp = httpx.post(_api_url(method), json=payload, timeout=35)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("Telegram %s failed", method)
        return None


def _todo_keyboard(todo_id: int, *, confirmed: bool) -> dict:
    if confirmed:
        buttons = [
            {"text": "✔ Done", "callback_data": f"done:{todo_id}"},
            {"text": "✖ Dismiss", "callback_data": f"dismiss:{todo_id}"},
        ]
    else:
        buttons = [
            {"text": "✅ Add to-do", "callback_data": f"confirm:{todo_id}"},
            {"text": "✖ Dismiss", "callback_data": f"dismiss:{todo_id}"},
        ]
    return {"inline_keyboard": [buttons]}


def _send_message(text: str, reply_markup: dict | None = None) -> None:
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _post("sendMessage", payload)


# ── outbound ──────────────────────────────────────────────────────────────────
def notify_new_todo(todo_id: int) -> None:
    if not settings.telegram_enabled:
        return
    with session_scope() as db:
        todo = db.get(Todo, todo_id)
        if todo is None or todo.status not in ("suggested", "confirmed"):
            return
        confirmed = todo.status == "confirmed"
        header = "🆕 <b>New to-do</b>" if confirmed else "💡 <b>Suggestion</b>"
        lines = [header, html.escape(todo.title)]
        if todo.reason:
            lines.append(f"<i>{html.escape(todo.reason)}</i>")
        if todo.due_date:
            lines.append(f"📅 due {todo.due_date:%Y-%m-%d}")
        if todo.source_url:
            lines.append(f'<a href="{html.escape(todo.source_url)}">open in Basecamp</a>')
        text = "\n".join(lines)
        keyboard = _todo_keyboard(todo.id, confirmed=confirmed)
    _send_message(text, keyboard)


def notify_reminder(todo_id: int) -> None:
    if not settings.telegram_enabled:
        return
    with session_scope() as db:
        todo = db.get(Todo, todo_id)
        if todo is None:
            return
        lines = ["⏰ <b>Reminder</b>", html.escape(todo.title)]
        if todo.due_date:
            lines.append(f"📅 due {todo.due_date:%Y-%m-%d}")
        if todo.source_url:
            lines.append(f'<a href="{html.escape(todo.source_url)}">open in Basecamp</a>')
        text = "\n".join(lines)
        keyboard = _todo_keyboard(todo.id, confirmed=True)
    _send_message(text, keyboard)


# ── inbound: inline button callbacks via long-poll ────────────────────────────
def _handle_callback(data: str) -> str:
    action, _, sid = data.partition(":")
    if not sid.isdigit():
        return "?"
    todo_id = int(sid)
    mapping = {"confirm": "confirmed", "dismiss": "dismissed", "done": "done"}
    new_status = mapping.get(action)
    if not new_status:
        return "?"
    with session_scope() as db:
        todo = db.get(Todo, todo_id)
        if todo is None:
            return "gone"
        todo.status = new_status
        title = todo.title
    verb = {"confirmed": "Added", "dismissed": "Dismissed", "done": "Done"}[new_status]
    return f"{verb}: {title[:60]}"


def _listen_loop() -> None:
    offset = 0
    log.info("Telegram listener started.")
    while True:
        try:
            resp = httpx.get(
                _api_url("getUpdates"),
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            resp.raise_for_status()
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                # Only honour button presses coming from the configured chat, so
                # the bot can't be driven by anyone else who happens to reach it.
                cq_chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                if cq_chat_id and cq_chat_id != str(settings.telegram_chat_id):
                    log.warning("Ignoring callback from unexpected chat %s", cq_chat_id)
                    _post("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    continue
                result_text = _handle_callback(cq.get("data", ""))
                # Acknowledge + reflect the outcome in the message.
                _post("answerCallbackQuery", {
                    "callback_query_id": cq["id"],
                    "text": result_text,
                })
                msg = cq.get("message", {})
                if msg:
                    _post("editMessageText", {
                        "chat_id": msg["chat"]["id"],
                        "message_id": msg["message_id"],
                        "text": f"{msg.get('text','')}\n\n➡️ <b>{html.escape(result_text)}</b>",
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    })
        except Exception:
            log.exception("Telegram listener error; backing off 5s")
            time.sleep(5)


def start_listener() -> threading.Thread | None:
    """Start the callback listener in a daemon thread (no-op if not configured)."""
    if not settings.telegram_enabled:
        log.info("Telegram not configured — listener not started.")
        return None
    thread = threading.Thread(target=_listen_loop, name="telegram-listener", daemon=True)
    thread.start()
    return thread
