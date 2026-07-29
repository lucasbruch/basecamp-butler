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
                retry_after = float(resp.headers.get("Retry-After", "5"))
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
        max_pages: int = 5,
    ) -> list:
        """Lines of one chat, fetching only as far back as the caller needs.

        `since_id` is the caller's watermark — the highest line id already
        ingested. We stop paging the moment a page reaches back to it, because
        everything beyond is by definition already stored.

        That guard matters: a room with history always advertises a `Link: next`,
        so paging blindly to `max_pages` cost five requests per room *and* per
        Ping thread on every single poll, almost always to re-fetch lines we
        already had. With a watermark the steady state is one request.

        `since_id=None` means first sight of this room — the caller only seeds a
        watermark from the newest id and ingests nothing, so one page is plenty.

        Returns a flat list; the caller filters by id, so ordering across pages
        doesn't matter.
        """
        path = f"buckets/{bucket_id}/chats/{chat_id}/lines.json"
        if since_id is None:
            max_pages = 1

        url: str | None = path
        collected: list = []
        pages = 0
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
        return collected


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
