"""The web auth gate: Bearer / Basic / ?token acceptance, constant-time compare."""
import base64

import pytest

from app.config import settings
from app.web import routes


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, headers=None, query=None, path="/"):
        self.headers = headers or {}
        self.query_params = query or {}
        self.url = _FakeURL(path)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    return "s3cret"


def test_bearer_ok(secret):
    req = _FakeRequest(headers={"Authorization": "Bearer s3cret"})
    assert routes._request_authorized(req)


def test_basic_ok(secret):
    creds = base64.b64encode(b"anyuser:s3cret").decode()
    req = _FakeRequest(headers={"Authorization": f"Basic {creds}"})
    assert routes._request_authorized(req)


def test_query_token_ok(secret):
    req = _FakeRequest(query={"token": "s3cret"})
    assert routes._request_authorized(req)


def test_wrong_and_missing_rejected(secret):
    assert not routes._request_authorized(_FakeRequest(headers={"Authorization": "Bearer nope"}))
    assert not routes._request_authorized(_FakeRequest())
    bad = base64.b64encode(b"u:wrong").decode()
    assert not routes._request_authorized(_FakeRequest(headers={"Authorization": f"Basic {bad}"}))
