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

from .. import runtime, todos as todo_actions
from ..config import settings
from ..db import session_scope
from ..models import Todo
from ..util import due_on

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
        primary = [
            {"text": "✔ Done", "callback_data": f"done:{todo_id}"},
            {"text": "✖ Dismiss", "callback_data": f"dismiss:{todo_id}"},
        ]
    else:
        primary = [
            {"text": "✅ Add to-do", "callback_data": f"confirm:{todo_id}"},
            {"text": "✖ Dismiss", "callback_data": f"dismiss:{todo_id}"},
        ]
    # Telegram has room for a second row, unlike ntfy's three-action cap, so the
    # snooze presets go here rather than displacing Dismiss.
    snooze = [
        {"text": "💤 1h", "callback_data": f"snooze-1h:{todo_id}"},
        {"text": "💤 Tomorrow", "callback_data": f"snooze-tomorrow:{todo_id}"},
        {"text": "💤 Next week", "callback_data": f"snooze-week:{todo_id}"},
    ]
    return {"inline_keyboard": [primary, snooze]}


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
def send_text(title: str, message: str) -> None:
    """Push a plain-text notification (e.g. an on-demand report) to the chat."""
    if not settings.telegram_enabled:
        return
    _send_message(f"<b>{html.escape(title)}</b>\n{html.escape(message)}")


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
        day = due_on(todo.due_date, runtime.load(db).tz, all_day=todo.due_all_day)
        if day:
            lines.append(f"📅 due {day:%Y-%m-%d}")
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
        day = due_on(todo.due_date, runtime.load(db).tz, all_day=todo.due_all_day)
        if day:
            lines.append(f"📅 due {day:%Y-%m-%d}")
        if todo.source_url:
            lines.append(f'<a href="{html.escape(todo.source_url)}">open in Basecamp</a>')
        text = "\n".join(lines)
        keyboard = _todo_keyboard(todo.id, confirmed=True)
    _send_message(text, keyboard)


# ── inbound: inline button callbacks via long-poll ────────────────────────────
def _handle_callback(data: str) -> str:
    """Apply a button press. Shares `todos.apply_action` with the web UI, so a
    press here has exactly the same effect as a click there."""
    action, _, sid = data.partition(":")
    if not sid.isdigit() or action not in todo_actions.ALL_ACTIONS:
        return "?"
    todo_id = int(sid)
    with session_scope() as db:
        cfg = runtime.load(db)
        todo = todo_actions.apply_action(db, todo_id, action, cfg)
        if todo is None:
            return "gone"
        title = todo.title
        snoozed = todo.snoozed_until

    if action in todo_actions.SNOOZE_ACTIONS:
        when = snoozed.astimezone(cfg.tz).strftime("%a %H:%M") if snoozed else ""
        return f"Snoozed until {when}: {title[:50]}"
    verb = {
        "confirm": "Added", "dismiss": "Dismissed",
        "done": "Done", "reopen": "Reopened",
    }[action]
    result = f"{verb}: {title[:60]}"

    # Confirming from the phone should reach Basecamp too, exactly as it does
    # from the web UI — otherwise the same button means two different things.
    if action == "confirm":
        try:
            from .. import writeback

            writeback.push(todo_id)
        except Exception:
            log.exception("Write-back failed for todo %s", todo_id)
    return result


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
