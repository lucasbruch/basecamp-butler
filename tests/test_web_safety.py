"""Redirect and cross-origin handling on the mutating routes.

Both guards matter because the UI is normally protected with HTTP Basic, and
browsers attach Basic credentials to cross-site form POSTs — so an unguarded
POST route is drivable by any page the user happens to visit.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.web import routes


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _req(referer=None, host="butler.local:8000", origin=None):
    headers = {"Host": host}
    if referer is not None:
        headers["Referer"] = referer
    if origin is not None:
        headers["Origin"] = origin
    return _FakeRequest(headers)


# ── _safe_redirect ───────────────────────────────────────────────────────────
def test_same_host_referer_keeps_path_and_query():
    dest = routes._safe_redirect(_req("http://butler.local:8000/todos?status=suggested"))
    assert dest == "/todos?status=suggested"


def test_foreign_referer_falls_back():
    """The open redirect: a page elsewhere could POST here and bounce the user
    back to itself, carrying whatever it liked in the URL."""
    assert routes._safe_redirect(_req("https://evil.example/hook")) == "/"


def test_protocol_relative_referer_is_not_a_local_path():
    # urlsplit reads "//evil.example/x" as a netloc, which must not pass.
    assert routes._safe_redirect(_req("//evil.example/x")) == "/"


def test_missing_referer_uses_the_fallback():
    assert routes._safe_redirect(_req(), fallback="/settings") == "/settings"


def test_relative_referer_is_allowed():
    assert routes._safe_redirect(_req("/activity?kind=llm")) == "/activity?kind=llm"


# ── _same_origin ─────────────────────────────────────────────────────────────
def test_matching_origin_passes():
    assert routes._same_origin(_req(origin="http://butler.local:8000"))


def test_foreign_origin_blocked():
    assert not routes._same_origin(_req(origin="https://evil.example"))


def test_absent_origin_allowed_for_non_browser_clients():
    """The ntfy action buttons send no Origin and authenticate with a Bearer
    token; they have no ambient credentials for a drive-by to borrow."""
    assert routes._same_origin(_req())


# ── end to end through the real middleware ───────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "", raising=False)
    return TestClient(routes.create_app())


def test_cross_origin_post_is_refused(client):
    resp = client.post(
        "/api/todos/1/confirm",
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_unknown_action_rejected(client):
    resp = client.post("/api/todos/1/explode")
    assert resp.status_code == 400
