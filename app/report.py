"""On-demand activity report: condense the last N hours of Basecamp activity
into a short, one-screen briefing.

Triggered from the /report page (an hour slider + a Generate button). When the
local LLM is reachable it synthesises a concise plain-text report; otherwise we
fall back to a deterministic structured summary so the button always returns
something useful, even without Ollama.

The LLM here is used for free-text prose (not the JSON verdict contract the
classifier uses), so it has its own small prompt and call helper rather than
reusing `classifier.ollama`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import runtime
from .classifier import conversation
from .classifier.ollama import DEFAULT_ROLE
from .config import settings
from .db import session_scope
from .models import AppState, Project, RawEvent, Report, Todo
from .util import utcnow

log = logging.getLogger(__name__)

# Sentinel: Ollama could not be reached (host asleep / not configured). Distinct
# from None (a reachable model that returned nothing usable) so the caller can
# word the fallback note appropriately.
_UNREACHABLE = object()

_TAG_RE = re.compile(r"<[^>]+>")

# Allowed slider range, and the default when none is given.
MIN_HOURS = 1
MAX_HOURS = 72
DEFAULT_HOURS = 24

# Keep the LLM input bounded: a busy 72h window could otherwise be huge.
MAX_EVENTS = 300
MAX_TODOS = 200
MAX_DIGEST_CHARS = 7000
BODY_TRUNC = 240

# Archived briefings are a convenience, not an audit trail.
MAX_STORED_REPORTS = 60

# The report system prompt. Only {role} and {window} vary; the structure stays
# fixed so the output is predictably short and skimmable.
_REPORT_TEMPLATE = """\
You are {role}. Write a short briefing for the account owner from the Basecamp \
activity below, covering the last {window}.

Rules:
- Be concise. The whole report MUST fit on one screen — aim for under 180 words.
- Plain text only: no markdown, no #, no **, no code fences. For lists use short \
lines that begin with "- ".
- Line 1 is "TL;DR: " followed by one sentence summarising the period.
- Then, only if warranted, a "Needs your attention:" block — the few items \
actually aimed at the owner or time-sensitive.
- Then "Highlights:" — one short line per project or thread that matters. Skip \
pure chatter and FYIs.
- Use only what's in the data. Invent nothing. If it was quiet, say so plainly \
in the TL;DR and keep the rest empty.
- Name people where the data names them.\
"""


def clamp_hours(hours: object) -> int:
    """Coerce an arbitrary input into a valid hour count within the slider range."""
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return max(MIN_HOURS, min(MAX_HOURS, h))


def humanize_hours(hours: int) -> str:
    if hours == 24:
        return "24 hours"
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _strip(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _persona_role(db: Session) -> str:
    """The user's configured character, so the briefing voice matches the app."""
    row = db.get(AppState, "llm_role")
    return (row.value.strip() if row and row.value else "") or DEFAULT_ROLE


def _gather(db: Session, hours: int):
    """The window's activity, newest-biased.

    Both queries take the *newest* N rows and then flip back to chronological
    order. Selecting ascending and limiting would keep the oldest N — on a busy
    72h window that summarises Monday morning and silently drops everything
    since, which is precisely backwards for a "what did I miss" briefing.
    """
    since = utcnow() - timedelta(hours=hours)
    events = list(
        reversed(
            db.execute(
                select(RawEvent)
                .where(RawEvent.updated_at >= since)
                .order_by(RawEvent.updated_at.desc())
                .limit(MAX_EVENTS)
            )
            .scalars()
            .all()
        )
    )
    todos = list(
        reversed(
            db.execute(
                select(Todo)
                .where(Todo.created_at >= since)
                .order_by(Todo.created_at.desc())
                .limit(MAX_TODOS)
            )
            .scalars()
            .all()
        )
    )
    names = {p.id: p.name for p in db.execute(select(Project)).scalars()}
    return since, events, todos, names


_TYPE_LABELS = {
    "message": "Messages",
    "comment": "Comments",
    "ping": "Pings",
    "chat": "Campfire",
    "todo": "To-dos",
    "todolist": "To-do lists",
}


def _count_types(events: list[RawEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e.type] = counts.get(e.type, 0) + 1
    return counts


def _proj(names: dict[int, str], pid: int | None) -> str:
    return names.get(pid, "General") if pid else "General"


def _sender(event: RawEvent) -> str:
    return ((event.payload or {}).get("creator") or {}).get("name", "").strip()


def _event_line(event: RawEvent, names: dict[int, str]) -> str:
    p = event.payload or {}
    subject = _strip(p.get("subject") or p.get("title") or "")
    body = _strip(p.get("content") or p.get("content_excerpt") or "")
    who = _sender(event)
    head = f"[{_proj(names, event.project_id)}] {event.type}"
    if who:
        head += f" by {who}"
    tail = subject or body
    if subject and body:
        tail = f"{subject}: {body}"
    return f"{head} — {tail[:BODY_TRUNC]}".rstrip(" —")


def _thread_block(group: list[RawEvent], names: dict[int, str], label: str) -> str:
    lines = [f"[{_proj(names, group[0].project_id)}] {label} thread:"]
    for ev in group:
        who = _sender(ev) or "Someone"
        body = _strip((ev.payload or {}).get("content") or (ev.payload or {}).get("content_excerpt"))
        if body:
            lines.append(f"  {who}: {body[:BODY_TRUNC]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_digest(events: list[RawEvent], todos: list[Todo], names: dict[int, str]) -> str:
    """A compact, LLM-friendly transcript of the window, bounded in size.

    Pings and Campfire lines are grouped per thread (a single ask often spans
    several lines); everything else is one line per event. Blocks are emitted in
    time order, and when the digest has to be cut it drops the *oldest* blocks —
    slicing the joined string instead would have thrown away whichever category
    happened to be serialised last (which was always the conversations).
    """
    threads: list[RawEvent] = []
    singles: list[RawEvent] = []
    for e in events:
        (threads if e.type in ("ping", "chat") else singles).append(e)

    # (sort key, text). A thread sorts by its most recent line, so an ongoing
    # conversation lands where it actually belongs in the timeline.
    blocks: list[tuple[datetime, str]] = []
    for e in singles:
        line = _event_line(e, names)
        if line:
            blocks.append((e.updated_at, line))
    for _chat_id, group in conversation.group_by_thread(threads):
        label = "Ping" if group[0].type == "ping" else "Campfire"
        block = _thread_block(group, names, label)
        if block:
            blocks.append((group[-1].updated_at, block))

    blocks.sort(key=lambda pair: pair[0])

    # Fill the budget from the newest end, then restore chronological order.
    kept: list[str] = []
    budget = MAX_DIGEST_CHARS
    truncated = False
    for _when, text in reversed(blocks):
        cost = len(text) + 1
        if cost > budget:
            truncated = True
            break
        budget -= cost
        kept.append(text)
    kept.reverse()

    if truncated:
        kept.insert(0, "… (older activity omitted)")

    if todos:
        raised = ["To-dos the butler raised this period:"]
        for t in todos:
            raised.append(f"  - {t.title[:BODY_TRUNC]}")
        kept.append("\n".join(raised))

    return "\n".join(kept)


def _ask_ollama_report(system_prompt: str, user_text: str):
    """Return the report text, None (reachable but unusable), or _UNREACHABLE."""
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "system": system_prompt,
                "prompt": user_text,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=120,
            proxy=settings.ollama_proxy or None,
        )
        resp.raise_for_status()
        text = (resp.json().get("response") or "").strip()
        return text or None
    except httpx.RequestError:
        return _UNREACHABLE
    except Exception:
        log.exception("Ollama report generation failed")
        return None


def _fallback_report(
    events: list[RawEvent],
    todos: list[Todo],
    names: dict[int, str],
    counts: dict[str, int],
    window: str,
) -> str:
    """Deterministic one-screen summary, used when the LLM is unavailable."""
    projects = {e.project_id for e in events if e.project_id}
    lines = [
        f"TL;DR: {len(events)} item(s) across {len(projects) or 1} project(s) "
        f"in the last {window}."
    ]

    breakdown = [
        f"{label}: {counts[t]}" for t, label in _TYPE_LABELS.items() if counts.get(t)
    ]
    if breakdown:
        lines.append("- " + ", ".join(breakdown))

    if todos:
        lines.append("")
        lines.append("Needs your attention:")
        for t in todos[:10]:
            lines.append(f"- {t.title[:120]}")
        if len(todos) > 10:
            lines.append(f"- …and {len(todos) - 10} more")

    # Per-project highlight: count + the most recent subject/body in that project.
    by_project: dict[str, list[RawEvent]] = {}
    for e in events:
        by_project.setdefault(_proj(names, e.project_id), []).append(e)
    if by_project:
        lines.append("")
        lines.append("Highlights:")
        for name, evs in sorted(by_project.items(), key=lambda kv: -len(kv[1]))[:10]:
            last = evs[-1].payload or {}
            snippet = _strip(
                last.get("subject") or last.get("title")
                or last.get("content") or last.get("content_excerpt") or ""
            )
            tail = f" — latest: {snippet[:100]}" if snippet else ""
            lines.append(f"- {name}: {len(evs)} item(s){tail}")

    return "\n".join(lines)


def store(db: Session, result: dict, *, scheduled: bool = False) -> int | None:
    """Persist a generated report so it can be re-read later.

    The briefing used to exist only in the browser tab that asked for it, which
    made "what did I miss on Tuesday" unanswerable. Best-effort: a failure to
    archive must not lose the report the user is waiting on.
    """
    try:
        row = Report(
            hours=int(result.get("hours") or DEFAULT_HOURS),
            source=str(result.get("source") or "summary")[:20],
            # String(100), and the model name comes from the environment.
            model=str(result["model"])[:100] if result.get("model") else None,
            event_count=int(result.get("event_count") or 0),
            todo_count=int(result.get("todo_count") or 0),
            body=result.get("report") or "",
            scheduled=scheduled,
        )
        db.add(row)
        db.flush()
        _prune(db)
        return row.id
    except Exception:
        log.exception("Could not archive the generated report")
        return None


def _prune(db: Session, keep: int = MAX_STORED_REPORTS) -> None:
    ids = (
        db.execute(select(Report.id).order_by(Report.id.desc()).limit(keep))
        .scalars()
        .all()
    )
    if len(ids) >= keep:
        db.execute(delete(Report).where(Report.id < ids[-1]))


def run_daily(hours: int | None = None) -> dict | None:
    """Generate the scheduled briefing and push it. Returns the result dict.

    Skipped silently when the period was empty — a daily "nothing happened"
    notification is exactly the kind of thing that trains you to ignore the app.
    """
    from . import notifier

    with session_scope() as db:
        cfg = runtime.load(db)
        if not cfg.daily_report_enabled:
            return None
        result = generate_report(db, hours if hours is not None else cfg.daily_report_hours)
        store(db, result, scheduled=True)

    if result.get("source") == "empty":
        log.info("Daily report: quiet period, not pushing.")
        return result

    window = result.get("window") or ""
    title = f"🫖 Daily briefing · last {window}" if window else "🫖 Daily briefing"
    if not notifier.notify_text(title, (result.get("report") or "")[:3800], cfg):
        log.info("Daily report generated but no push channel is configured.")
    return result


def generate_report(db: Session, hours: object) -> dict:
    """Build a condensed report of the last `hours` of activity.

    Never raises: returns a display-ready dict the /report page renders as-is.
    `source` is one of 'llm' | 'summary' | 'empty' so the UI can label it."""
    hours = clamp_hours(hours)
    window = humanize_hours(hours)
    _since, events, todos, names = _gather(db, hours)
    counts = _count_types(events)

    base = {
        "ok": True,
        "hours": hours,
        "window": window,
        "event_count": len(events),
        "todo_count": len(todos),
        "generated_at": utcnow().isoformat(),
        "model": None,
    }

    if not events and not todos:
        return {
            **base,
            "source": "empty",
            "report": f"TL;DR: Quiet period — no Basecamp activity in the last {window}.",
        }

    digest = _build_digest(events, todos, names)
    system_prompt = _REPORT_TEMPLATE.format(role=_persona_role(db), window=window)
    verdict = _ask_ollama_report(system_prompt, digest)

    if verdict is _UNREACHABLE or verdict is None:
        note = (
            "(LLM offline — showing a plain summary)"
            if verdict is _UNREACHABLE
            else "(LLM unavailable — showing a plain summary)"
        )
        return {
            **base,
            "source": "summary",
            "note": note,
            "report": _fallback_report(events, todos, names, counts, window),
        }

    return {**base, "source": "llm", "model": settings.ollama_model, "report": verdict}
