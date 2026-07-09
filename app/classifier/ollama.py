"""Local LLM (v2) classifier via Ollama. Optional — enable with CLASSIFIER=ollama.

Kept intentionally small: it summarises a batch of new activity and decides,
per item, whether it warrants a to-do. The system prompt frames the model as a
producer/coordinator-level expert in VFX, full-CG commercial production and DOOH
so its language and judgement match the pipeline.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import RawEvent, Todo
from .rules import _already_have_todo, _auto_add  # reuse helpers

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

SYSTEM_PROMPT = """\
You are a senior VFX/CG producer-coordinator assistant. You are fluent in the \
pipeline and vocabulary of visual effects, full-CG commercial production, and \
specialty Digital Out-of-Home (DOOH): shots, sequences, comp, render passes, \
lookdev, lighting, FX/sim, color grade, conform, client review rounds, \
revisions, deliverables and delivery specs, DOOH loops/specs/resolutions.

Given one item of Basecamp activity (a to-do, message, or comment), decide \
whether it implies an actionable to-do for the account owner. Respond ONLY with \
a compact JSON object:
  {"todo": true|false, "title": "<short imperative to-do>", "reason": "<why>"}
Use precise pipeline terminology. If it's just chatter/FYI, return todo=false.\
"""


def _text(html: str | None) -> str:
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()


def _summarise_event(event: RawEvent) -> str:
    p = event.payload or {}
    subject = _text(p.get("subject") or p.get("title") or "")
    content = _text(p.get("content") or "")
    return f"type={event.type}\nsubject={subject}\nbody={content}"[:4000]


def _ask_ollama(item_text: str) -> dict | None:
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "system": SYSTEM_PROMPT,
                "prompt": item_text,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        return json.loads(raw)
    except Exception:
        log.exception("Ollama classification failed for one item.")
        return None


def classify_events(db: Session) -> list[int]:
    events = (
        db.execute(
            select(RawEvent)
            .where(RawEvent.processed.is_(False))
            .order_by(RawEvent.updated_at.asc())
        )
        .scalars()
        .all()
    )

    created: list[int] = []
    for event in events:
        try:
            if _already_have_todo(db, event):
                continue
            verdict = _ask_ollama(_summarise_event(event))
            if verdict and verdict.get("todo"):
                p = event.payload or {}
                status = "confirmed" if _auto_add(db, event.project_id) else "suggested"
                todo = Todo(
                    source_event_id=event.id,
                    project_id=event.project_id,
                    title=(verdict.get("title") or "Suggested to-do")[:1000],
                    notes=verdict.get("reason"),
                    status=status,
                    reason="ollama",
                    source_url=p.get("app_url") or p.get("url"),
                )
                db.add(todo)
                db.flush()
                created.append(todo.id)
        finally:
            event.processed = True

    db.flush()
    if created:
        log.info("Ollama classifier created %d suggestion(s).", len(created))
    return created
