"""Thin Basecamp 3 REST client: auth header injection, pagination, rate limits."""
from __future__ import annotations

import logging
import time
from typing import Iterator

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Basecamp: 50 requests / 10 seconds per token. Space calls a touch to stay under.
MIN_INTERVAL = 10.0 / 50.0  # ~0.2s between requests
MAX_RETRIES = 5


class BasecampClient:
    def __init__(self, access_token: str, account_id: int, api_href: str | None = None):
        self.access_token = access_token
        self.account_id = account_id
        # api_href from authorization.json already includes the account id, e.g.
        # https://3.basecampapi.com/1234567
        self.base_url = (api_href or f"https://3.basecampapi.com/{account_id}").rstrip(
            "/"
        )
        self._last_request = 0.0
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": settings.basecamp_user_agent,
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BasecampClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── low-level ────────────────────────────────────────────────────────────
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)

    def _full_url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = self._full_url(path)
        for attempt in range(MAX_RETRIES):
            self._throttle()
            self._last_request = time.monotonic()
            resp = self._http.request(method, url, **kwargs)

            if resp.status_code == 429:
                retry_after = _retry_after(resp.headers.get("Retry-After"))
                log.warning("429 rate limited; sleeping %.1fs", retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code in (502, 503, 504):
                backoff = 2 ** attempt
                log.warning("HTTP %s; retrying in %ds", resp.status_code, backoff)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"Exhausted retries for {method} {url}")

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def get_json(self, path: str, **kwargs):
        return self.get(path, **kwargs).json()

    def paginate(self, path: str, *, max_pages: int | None = None, **kwargs) -> Iterator[dict]:
        """Yield every item across all pages, following Link: rel="next".

        `max_pages` bounds how deep we go (None = unbounded) — used for sources
        like Campfire where we only want to reach back far enough to cover a
        single poll interval, not the whole history.
        """
        url = path
        pages = 0
        while url and (max_pages is None or pages < max_pages):
            resp = self.get(url, **kwargs)
            pages += 1
            items = resp.json()
            if isinstance(items, list):
                yield from items
            else:  # a single object endpoint
                yield items
            url = _next_link(resp.headers.get("Link", ""))
            kwargs.pop("params", None)  # next URL already carries the query string

    # ── high-level helpers ────────────────────────────────────────────────────
    def my_profile(self) -> dict:
        return self.get_json("my/profile.json")

    def projects(self) -> Iterator[dict]:
        return self.paginate("projects.json")

    def recordings(self, rec_type: str, bucket_ids: list[int] | None = None) -> Iterator[dict]:
        """List recordings of a type across buckets, newest first.

        rec_type: Todo | Message | Comment | Todolist | Document | Upload | ...
        This single endpoint is how we cheaply detect "what changed" without
        walking every to-do list per project.
        """
        params = {"type": rec_type, "sort": "updated_at", "direction": "desc"}
        if bucket_ids:
            params["bucket"] = ",".join(str(b) for b in bucket_ids)
        return self.paginate("projects/recordings.json", params=params)

    def project(self, project_id: int) -> dict:
        """One project, including its `dock` — the list of enabled tools."""
        return self.get_json(f"projects/{project_id}.json")

    def todolists(self, project_id: int) -> list[dict]:
        """Every to-do list in a project's to-do set.

        Two hops, because Basecamp models it that way: the project's dock names
        a `todoset`, and the lists hang off that. Returns [] when the project has
        the to-dos tool switched off, which is a normal state, not an error.
        """
        dock = self.project(project_id).get("dock") or []
        todoset = next(
            (d for d in dock if d.get("name") == "todoset" and d.get("enabled")), None
        )
        if not todoset or not todoset.get("url"):
            return []
        todoset_data = self.get_json(todoset["url"])
        lists_url = todoset_data.get("todolists_url")
        if not lists_url:
            return []
        return list(self.paginate(lists_url))

    def create_todo(
        self,
        project_id: int,
        todolist_id: int,
        content: str,
        *,
        description: str | None = None,
        due_on: str | None = None,
    ) -> dict:
        """Create a real Basecamp to-do. `due_on` is an ISO date (YYYY-MM-DD)."""
        payload: dict = {"content": content[:500]}
        if description:
            payload["description"] = description
        if due_on:
            payload["due_on"] = due_on
        resp = self.request(
            "POST",
            f"buckets/{project_id}/todolists/{todolist_id}/todos.json",
            json=payload,
        )
        return resp.json()

    def create_chat_line(self, bucket_id: int, chat_id: int, content: str) -> dict:
        """Post a line into a chat — a Campfire room or a Ping conversation.

        Same bucket-scoped path we read lines from, so a Circle (where Pings
        live) is addressed exactly like a project. Basecamp answers 201 with the
        created line.

        `content` is **plain text**, and that is not what the API docs say: they
        call a chat line rich text and list ``<br>`` among the tags allowed. A
        Ping sent that way arrives with the markup showing — a reply with a
        blank line in it reads "…fear of climbing<br><br>(written by…)" to the
        person on the other end. Whatever the docs describe, what this endpoint
        does with a user's token is escape the tag and show it.

        So nothing is escaped on the way in either: an ``&`` sent as ``&amp;``
        would arrive as ``&amp;`` for the same reason. Newlines are newlines.
        A to-do description *is* rich text and still goes through `as_html`;
        this is the one path where that would be wrong.
        """
        resp = self.request(
            "POST",
            f"buckets/{bucket_id}/chats/{chat_id}/lines.json",
            json={"content": content},
        )
        # 201 normally carries the record; tolerate an empty body regardless.
        return resp.json() if resp.content else {}

    def my_readings(self, page: int = 1) -> dict:
        """The account-wide notifications feed (unreads/reads/memories).

        This is how Pings surface: entries with section == "pings" live in
        `Circle` buckets and never appear in projects/recordings.json.
        """
        return self.get_json("my/readings.json", params={"page": page})

    def campfires(self) -> Iterator[dict]:
        """List Campfire chat rooms the user can see (one or more per project)."""
        return self.paginate("chats.json")

    def chat_lines(
        self,
        bucket_id: int,
        chat_id: int,
        *,
        since_id: int | None = None,
        max_pages: int = 20,
    ) -> tuple[list, bool]:
        """Lines of one chat, fetching only as far back as the caller needs.

        Returns ``(lines, complete)``. `complete` is False only when the page
        budget ran out with more history still to walk — i.e. the caller is
        looking at a hole, not at everything since its watermark.

        `since_id` is the caller's watermark — the highest line id already
        ingested. We stop paging the moment a page reaches back to it, because
        everything beyond is by definition already stored.

        That guard matters: a room with history always advertises a `Link: next`,
        so paging blindly to `max_pages` cost five requests per room *and* per
        Ping thread on every single poll, almost always to re-fetch lines we
        already had. With a watermark the steady state is one request — which is
        why `max_pages` can afford to be generous. It binds only while catching
        up, and that is exactly when being stingy silently drops messages: the
        caller moves its watermark to the newest id it was handed, so anything
        below an exhausted budget is never asked for again.

        `since_id=None` means first sight of this room, minutes-old in the
        ordinary case — one page, and the caller bounds it by age anyway.

        Basecamp serves chat lines **newest first**, which is what makes the
        watermark break above work — but it is the opposite of the order the
        conversation happened in. Callers store one row per line as they walk
        this list, so handing it back raw stamped a burst of messages with row
        ids running backwards in time, and every later reader that trusted
        "highest row id = last thing said" got the *first* line of the burst.
        That is how a reply to somebody could conclude your own earlier message
        was the newest one in the thread. So the pages are stitched back into
        chronological order here, once, rather than left for each caller to
        remember: oldest → newest, by line id.
        """
        path = f"buckets/{bucket_id}/chats/{chat_id}/lines.json"
        if since_id is None:
            max_pages = 1

        url: str | None = path
        collected: list = []
        pages = 0
        complete = True
        while url and pages < max_pages:
            resp = self.get(url)
            pages += 1
            items = resp.json()
            if not isinstance(items, list) or not items:
                break
            collected.extend(items)
            if since_id is not None and any(
                item.get("id", 0) <= since_id for item in items
            ):
                break  # reached known ground
            url = _next_link(resp.headers.get("Link", ""))
            if url and pages >= max_pages and since_id is not None:
                # More history is on offer and we've run out of budget for it.
                complete = False
                log.warning(
                    "Chat %s: stopped after %d page(s) with older lines still "
                    "unread — everything before line %s is a gap.",
                    chat_id,
                    pages,
                    min((i.get("id", 0) for i in collected), default="?"),
                )
        collected.sort(key=lambda item: item.get("id") or 0)
        return collected, complete


def _retry_after(value: str | None, default: float = 5.0) -> float:
    """Seconds to wait after a 429.

    Basecamp sends a plain number, but the header is also allowed to carry an
    HTTP-date, and anything in front of the API (a CDN, a reverse proxy) may
    send one. Parsing that as a float raised straight out of the retry loop and
    failed the whole poll cycle over a header we only use to pick a sleep.
    Bounded so a hostile or garbled value can't park the poller for an hour.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(seconds, 60.0))


def _next_link(link_header: str) -> str | None:
    """Parse a Link header and return the rel="next" URL, if any."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        for seg in segments[1:]:
            if seg.strip() == 'rel="next"':
                return url
    return None


def client_for(db) -> "BasecampClient | None":
    """A ready client for the stored OAuth token, or None when Basecamp isn't
    connected yet.

    Both the write-back and the auto-reply paths need one of these outside the
    poller, and both used to build it by hand; a single factory keeps the
    "no token / no account id yet" answer identical everywhere.
    """
    from .auth import get_token_row, get_valid_access_token

    try:
        token = get_token_row(db)
    except RuntimeError:
        return None
    if not token.account_id:
        return None
    access = get_valid_access_token(db)
    token = get_token_row(db)  # refresh may have rewritten the row
    return BasecampClient(access, token.account_id, token.api_href)
