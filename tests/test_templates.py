"""Render every template against realistic data.

There's no Postgres in the test environment, so the routes can't be exercised end
to end. These render the same templates through the real Jinja environment (with
the real `timeago` / `localtime` filters) using objects shaped like the ORM rows,
which catches the class of breakage a template change actually causes: a renamed
context key, a filter called with the wrong arity, an attribute that no longer
exists on the model.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import runtime, todos as todo_actions
from app.config import settings
from app.web.routes import TEMPLATES, _localtime, _timeago

NOW = datetime.now(timezone.utc)


class Obj:
    """Minimal stand-in for an ORM row."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def todo(**over):
    base = dict(
        id=1, title="Send the grading notes to Ana", notes="She asked on Friday.",
        status="suggested", reason="ping", project_id=100,
        due_date=NOW + timedelta(days=2), source_url="https://3.basecamp.com/1/x",
        basecamp_url=None, snoozed_until=None, created_at=NOW, updated_at=NOW,
        completed_at=None, thread_key="7",
    )
    base.update(over)
    return Obj(**base)


def cfg(**over):
    return runtime.RuntimeConfig(**{**runtime.defaults(), **over})


def render(name, ctx):
    template = TEMPLATES.env.get_template(name)
    return template.render(**ctx)


@pytest.fixture
def base_ctx():
    # `request` is required by Jinja2Templates' url_for machinery only when
    # templates use it; ours don't, so a placeholder is enough.
    return {"request": None, "tz": "Europe/Berlin",
            "snooze_actions": todo_actions.SNOOZE_ACTIONS,
            "zone_groups": runtime.zone_groups("Europe/Berlin")}


def test_dashboard_renders(base_ctx):
    html = render("index.html", {
        **base_ctx,
        "suggested": [todo()],
        "confirmed": [todo(id=2, status="confirmed", title="Book the grade suite")],
        "snoozed": [todo(id=3, snoozed_until=NOW + timedelta(hours=5))],
        "projects": {100: "Feature Film"},
        "suggest_max": 3,
        "status": {
            "last_poll_at": NOW, "last_poll_new": "2", "last_poll_ok": "1",
            "last_poll_error": "", "pings_checked_at": NOW, "pings_visible": "3",
            "llm_status": "ok", "llm_checked_at": NOW, "classifier": "ollama",
            "poll_pings": True, "quiet_now": True, "writeback": True,
        },
    })
    assert "Send the grading notes to Ana" in html
    assert 'data-todo-id="1"' in html
    assert "Quiet hours" in html
    assert "Write-back" in html
    assert "💤 Snoozed (1)" in html


def test_dashboard_renders_when_everything_is_empty(base_ctx):
    html = render("index.html", {
        **base_ctx,
        "suggested": [], "confirmed": [], "snoozed": [], "projects": {},
        "suggest_max": 0,
        "status": {
            "last_poll_at": None, "last_poll_new": None, "last_poll_ok": None,
            "last_poll_error": None, "pings_checked_at": None, "pings_visible": None,
            "llm_status": None, "llm_checked_at": None, "classifier": "rules",
            "poll_pings": False, "quiet_now": False, "writeback": False,
        },
    })
    assert "The butler is watching" in html
    assert "never" in html  # the timeago filter's null case


def _replies_ctx(base_ctx, **over):
    ctx = {
        **base_ctx,
        "drafts": [], "history": [], "rules": [], "sent_today": 0,
        "cfg": cfg(autoreply_enabled=True),
        "decisions": [], "last_pass": None,
        "checks": [{"level": "ok", "text": "Nothing is standing in the way."}],
        "status": {
            "last_poll_at": NOW, "last_poll_new": "0", "last_poll_ok": "1",
            "last_poll_error": "", "pings_checked_at": NOW, "pings_visible": "2",
            "llm_status": "ok", "llm_checked_at": NOW, "classifier": "ollama",
            "poll_pings": True, "quiet_now": False, "writeback": False,
        },
    }
    ctx.update(over)
    return ctx


def test_replies_page_says_what_it_decided(base_ctx):
    """The page has to answer "why didn't it reply?" without anyone reading
    container logs — that question is the reason the section exists."""
    html = render("replies.html", _replies_ctx(
        base_ctx,
        decisions=[{"chat_id": "7", "person": "Ana", "at": NOW,
                    "why": "Already answered inside the 30-minute window."}],
        last_pass={"why": "Looked at 1 Ping conversation and composed 0.",
                   "at": NOW, "threads": 1, "composed": 0},
    ))
    assert "Why it is quiet" in html
    assert "Ana" in html
    assert "30-minute window" in html
    assert "Looked at 1 Ping conversation" in html


def test_replies_page_flags_the_upstream_failures(base_ctx):
    """A butler that never saw the Ping and one that saw it and stayed out of it
    are different problems, and the page must not conflate them."""
    html = render("replies.html", _replies_ctx(base_ctx, status={
        "last_poll_at": None, "last_poll_new": None, "last_poll_ok": None,
        "last_poll_error": None, "pings_checked_at": None, "pings_visible": None,
        "llm_status": "unreachable", "llm_checked_at": None, "classifier": "ollama",
        "poll_pings": True, "quiet_now": True, "writeback": False,
    }))
    assert "the poller hasn't reached them" in html
    assert "unreachable" in html
    assert "quiet hours" in html


def test_replies_page_leads_with_the_self_check(base_ctx):
    """The check that names the cause has to be the first thing on the page —
    the decisions below it are evidence, not a diagnosis."""
    html = render("replies.html", _replies_ctx(base_ctx, checks=[
        {"level": "problem", "text": "Nobody is on the auto-reply list."},
        {"level": "warn", "text": "Pinged you recently: “Ana Müller”"},
        {"level": "ok", "text": "The LLM is answering."},
    ]))
    assert "Nobody is on the auto-reply list" in html
    assert "Ana Müller" in html
    assert html.index("Nobody is on the auto-reply list") < html.index("Already handled")
    assert "Edit the list" in html  # offered, because something was wrong


def test_replies_page_offers_a_rewind_per_conversation(base_ctx):
    html = render("replies.html", _replies_ctx(base_ctx, decisions=[
        {"chat_id": "7", "person": "Ana", "at": NOW, "why": "Nothing new."},
    ]))
    assert 'data-again="7"' in html


def test_todos_page_renders_with_search_and_bulk(base_ctx):
    html = render("todos.html", {
        **base_ctx,
        "items": [todo(), todo(id=2, status="done", completed_at=NOW)],
        "projects": {100: "Feature Film"},
        "all_projects": [Obj(id=100, name="Feature Film")],
        "statuses": ("suggested", "confirmed", "dismissed", "done"),
        "active_status": "suggested", "active_project": 100, "query": "grading",
    })
    assert 'value="grading"' in html
    assert "bulk-pick" in html          # selection checkboxes
    assert 'data-bulk="dismiss"' in html


def test_settings_page_renders(base_ctx):
    html = render("settings.html", {
        **base_ctx,
        "projects": [Obj(id=100, name="Feature Film", enabled=True, auto_add=False,
                         last_polled_at=NOW, todolist_id=55, todolist_name="Post")],
        "settings": settings,
        "cfg": cfg(classifier="ollama", writeback_enabled=True),
        "env_defaults": runtime.defaults(),
        "overridden": {"poll_interval_minutes": "15"},
        "classifiers": runtime.CLASSIFIERS,
        "channels": runtime.CHANNELS,
        "telegram_enabled": False, "ntfy_enabled": True, "authorized": True,
        "muted": [Obj(id=1, name="Deploy Bot")],
        "assistant": {
            "role": "a post-production supervisor", "topics": "grading, VFX",
            "override": "", "default_role": "a helpful personal assistant",
            "default_topics": "everyday work", "active_prompt": "You are…",
            "feedback": "\n\nThey KEPT these:\n  + Send the grading notes",
        },
    })
    assert "Deploy Bot" in html
    assert "Overriding the environment for: poll_interval_minutes" in html
    assert 'name="poll_interval_minutes__present"' in html   # the checkbox marker
    assert "Send the grading notes" in html                  # learned examples
    # The project row must not be a <form> inside a <tr> — that was invalid HTML.
    assert "<tr>" not in html


def test_settings_names_the_zone_beside_the_times_it_governs(base_ctx):
    """"22 to 7 local" is only reassuring if you can see which local it means."""
    html = render("settings.html", {
        **base_ctx,
        "projects": [], "settings": settings,
        "cfg": cfg(timezone="Europe/Berlin", quiet_hours_start=22, quiet_hours_end=7,
                   daily_report_enabled=True, daily_report_hour=8),
        "env_defaults": runtime.defaults(), "overridden": {},
        "classifiers": runtime.CLASSIFIERS, "channels": runtime.CHANNELS,
        "telegram_enabled": False, "ntfy_enabled": False, "authorized": False,
        "muted": [],
        "assistant": {"role": "", "topics": "", "override": "",
                      "default_role": "", "default_topics": "",
                      "active_prompt": "", "feedback": ""},
    })
    assert html.count('<span class="env">Europe/Berlin</span>') == 2


def test_settings_offers_the_zones_as_a_dropdown(base_ctx):
    """Typing the zone by hand was the whole source of the spelling problem."""
    html = render("settings.html", {
        **base_ctx,
        "projects": [], "settings": settings, "cfg": cfg(timezone="Europe/Berlin"),
        "env_defaults": runtime.defaults(), "overridden": {},
        "classifiers": runtime.CLASSIFIERS, "channels": runtime.CHANNELS,
        "telegram_enabled": False, "ntfy_enabled": False, "authorized": False,
        "muted": [],
        "assistant": {"role": "", "topics": "", "override": "",
                      "default_role": "", "default_topics": "",
                      "active_prompt": "", "feedback": ""},
    })
    assert '<select id="timezone" name="timezone"' in html
    picker = html.split('<select id="timezone"')[1].split("</select>")[0]
    assert '<optgroup label="Europe">' in picker
    assert '<option value="Europe/Berlin" selected>Berlin</option>' in picker
    # Exactly one option is pre-selected, and it's the configured zone.
    assert picker.count(" selected>") == 1
    assert picker.count('value="America/Los_Angeles">Los Angeles<') == 1


def test_settings_says_when_a_zone_was_refused(base_ctx):
    html = render("settings.html", {
        **base_ctx,
        "projects": [], "settings": settings, "cfg": cfg(timezone="Europe/Berlin"),
        "env_defaults": runtime.defaults(), "overridden": {},
        "classifiers": runtime.CLASSIFIERS, "channels": runtime.CHANNELS,
        "telegram_enabled": False, "ntfy_enabled": False, "authorized": False,
        "muted": [], "rejected": {"timezone"},
        "assistant": {"role": "", "topics": "", "override": "",
                      "default_role": "", "default_topics": "",
                      "active_prompt": "", "feedback": ""},
    })
    assert "not a known zone" in html
    assert "kept Europe/Berlin" in html


def test_settings_renders_before_first_poll(base_ctx):
    html = render("settings.html", {
        **base_ctx,
        "projects": [], "settings": settings, "cfg": cfg(),
        "env_defaults": runtime.defaults(), "overridden": {},
        "classifiers": runtime.CLASSIFIERS, "channels": runtime.CHANNELS,
        "telegram_enabled": False, "ntfy_enabled": False, "authorized": False,
        "muted": [],
        "assistant": {"role": "", "topics": "", "override": "",
                      "default_role": "x", "default_topics": "y",
                      "active_prompt": "z", "feedback": ""},
    })
    assert "Basecamp is not connected yet" in html
    assert "No projects cached yet" in html
    assert "Nobody is muted" in html


def test_activity_page_renders(base_ctx):
    html = render("activity.html", {
        **base_ctx,
        "entries": [
            Obj(id=1, created_at=NOW, kind="llm", url=None,
                summary="LLM read a Ping conversation → suggests to-do",
                detail="prompt…"),
            Obj(id=2, created_at=NOW, kind="writeback",
                url="https://3.basecamp.com/1/y",
                summary="Added “Send the notes” to Basecamp (Post).", detail=None),
        ],
        "kinds": ("poll", "ping", "llm", "writeback", "error"),
        "active_kind": "llm", "query": "",
    })
    assert "writeback" in html
    assert "Show what was sent" in html


def test_report_page_renders_with_history(base_ctx):
    html = render("report.html", {
        **base_ctx,
        "min_hours": 1, "max_hours": 72, "default_hours": 24,
        "push_enabled": True, "notify_channel": "ntfy",
        "cfg": cfg(daily_report_enabled=True, daily_report_hour=8),
        "history": [Obj(id=1, created_at=NOW, hours=24, source="llm",
                        event_count=42, todo_count=3, scheduled=True)],
    })
    assert "Earlier briefings" in html
    assert 'data-report="1"' in html
    assert "08:00" in html


def test_report_page_renders_without_history(base_ctx):
    html = render("report.html", {
        **base_ctx,
        "min_hours": 1, "max_hours": 72, "default_hours": 24,
        "push_enabled": False, "notify_channel": "none",
        "cfg": cfg(daily_report_enabled=False), "history": [],
    })
    assert "Earlier briefings" not in html
    assert "Turn on the daily briefing" in html


# ── the filters the templates lean on ────────────────────────────────────────
def test_timeago_handles_past_present_and_future():
    assert _timeago(None) == "never"
    assert _timeago(NOW - timedelta(seconds=5)) == "just now"
    assert _timeago(NOW - timedelta(minutes=5)) == "5m ago"
    assert _timeago(NOW - timedelta(days=3)) == "3d ago"
    # A snooze is in the future; "0m ago" would have been nonsense.
    assert _timeago(NOW + timedelta(hours=5)) == "in 4h"


def test_localtime_shifts_into_the_configured_zone():
    stamp = datetime(2026, 7, 29, 22, 30, tzinfo=timezone.utc)
    assert _localtime(stamp, "UTC", "%H:%M") == "22:30"
    # Berlin is UTC+2 in July, so this is the next calendar day locally.
    assert _localtime(stamp, "Europe/Berlin", "%d %H:%M") == "30 00:30"


def test_localtime_tolerates_nulls_and_bad_zones():
    assert _localtime(None) == "—"
    assert _localtime(NOW, "Nowhere/Real")  # falls back to UTC rather than raising


def test_naive_datetimes_are_treated_as_utc():
    """A row predating the tz-aware columns (or a driver that drops the offset)
    must not turn one odd record into a 500 on the dashboard."""
    naive = datetime(2026, 7, 29, 12, 0)
    assert _localtime(naive, "UTC", "%H:%M") == "12:00"
    assert _timeago(naive) != "never"       # and doesn't raise
