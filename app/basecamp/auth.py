"""Basecamp 3 OAuth2 (37signals "Launchpad") auth + token refresh.

Flow reference: https://github.com/basecamp/api/blob/master/sections/authentication.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import OAuthToken

log = logging.getLogger(__name__)

LAUNCHPAD = "https://launchpad.37signals.com"
AUTH_URL = f"{LAUNCHPAD}/authorization/new"
TOKEN_URL = f"{LAUNCHPAD}/authorization/token"
AUTHORIZATION_JSON = f"{LAUNCHPAD}/authorization.json"

# Refresh a little before actual expiry to avoid mid-request failures.
REFRESH_SKEW = timedelta(minutes=30)


def build_authorize_url() -> str:
    """URL the user opens once to grant access."""
    params = {
        "type": "web_server",
        "client_id": settings.basecamp_client_id,
        "redirect_uri": settings.basecamp_redirect_uri,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Trade an authorization code for access + refresh tokens."""
    params = {
        "type": "web_server",
        "client_id": settings.basecamp_client_id,
        "client_secret": settings.basecamp_client_secret,
        "redirect_uri": settings.basecamp_redirect_uri,
        "code": code,
    }
    resp = httpx.post(TOKEN_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _refresh(refresh_token: str) -> dict:
    params = {
        "type": "refresh",
        "refresh_token": refresh_token,
        "client_id": settings.basecamp_client_id,
        "client_secret": settings.basecamp_client_secret,
    }
    resp = httpx.post(TOKEN_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def discover_account(access_token: str) -> tuple[int, str]:
    """Return (account_id, api_href) for the user's first bc3 account."""
    data = get_authorization(access_token)
    accounts = [a for a in data.get("accounts", []) if a.get("product") == "bc3"]
    if not accounts:
        raise RuntimeError("No Basecamp 3 (bc3) account found for this login.")
    acct = accounts[0]
    return acct["id"], acct["href"]


def get_authorization(access_token: str) -> dict:
    """GET /authorization.json — lists accounts the token can see."""
    resp = httpx.get(
        AUTHORIZATION_JSON,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": settings.basecamp_user_agent,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def store_token(db: Session, token_data: dict, *, account_id=None, api_href=None) -> OAuthToken:
    """Persist token payload from an exchange/refresh into the single-row table."""
    expires_in = int(token_data.get("expires_in", 1209600))  # default ~2 weeks
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    row = db.get(OAuthToken, 1)
    if row is None:
        row = OAuthToken(id=1)
        db.add(row)
    row.access_token = token_data["access_token"]
    # Refresh tokens are long-lived; a refresh response may omit it.
    if token_data.get("refresh_token"):
        row.refresh_token = token_data["refresh_token"]
    row.expires_at = expires_at
    if account_id is not None:
        row.account_id = account_id
    if api_href is not None:
        row.api_href = api_href
    db.flush()
    return row


def get_token_row(db: Session) -> OAuthToken:
    row = db.get(OAuthToken, 1)
    if row is None:
        raise RuntimeError(
            "No OAuth token stored. Run scripts/authorize.py first."
        )
    return row


def get_valid_access_token(db: Session) -> str:
    """Return a non-expired access token, refreshing in place if needed."""
    row = get_token_row(db)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at - REFRESH_SKEW:
        log.info("Access token near expiry — refreshing.")
        data = _refresh(row.refresh_token)
        row = store_token(db, data)
        db.commit()
    return row.access_token
