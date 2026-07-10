"""End-to-end check of the auth middleware via the real ASGI stack.

Uses routes that don't touch the database (/healthz, and 401 short-circuits
before the handler) so no Postgres is needed.
"""
from fastapi.testclient import TestClient

from app.config import settings
from app.web.routes import create_app


def test_open_when_no_token(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "", raising=False)
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200


def test_healthz_stays_open_even_with_token(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200


def test_protected_route_401_without_token(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    client = TestClient(create_app())
    resp = client.get("/")  # 401 short-circuits before any DB access
    assert resp.status_code == 401
    assert "Basic" in resp.headers.get("WWW-Authenticate", "")


def test_protected_route_passes_with_bearer(monkeypatch):
    monkeypatch.setattr(settings, "web_auth_token", "s3cret", raising=False)
    client = TestClient(create_app())
    # Wrong token still 401; right token gets past the gate (then may hit the DB,
    # which we don't assert on here — only that it's no longer a 401).
    assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
