"""Basecamp 3 OAuth2 (37signals "Launchpad") auth + token refresh.

Flow reference: https://github.com/basecamp/api/blob/master/sections/authentication.md
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppState, OAuthToken

log = logging.getLogger(__name__)

LAUNCHPAD = "https://launchpad.37signals.com"
AUTH_URL = f"{LAUNCHPAD}/authorization/new"
TOKEN_URL = f"{LAUNCHPAD}/authorization/token"
AUTHORIZATION_JSON = f"{LAUNCHPAD}/authorization.json"

# Refresh a little before actual expiry to avoid mid-request failures.
REFRESH_SKEW = timedelta(minutes=30)


STATE_KEY = "oauth_state"


def build_authorize_url(db: Session | None = None) -> str:
    """URL the user opens once to grant access.

    When a session is supplied we mint a one-shot `state` value and store it, so
    the callback can prove the code it receives belongs to a handshake *we*
    started. Without it, anyone who can reach the callback could feed us an
    authorization code for an account of their choosing and silently repoint the
    butler at it."""
    params = {
        "type": "web_server",
        "client_id": settings.basecamp_client_id,
        "redirect_uri": settings.basecamp_redirect_uri,
    }
    if db is not None:
        state = secrets.token_urlsafe(32)
        db.merge(AppState(key=STATE_KEY, value=state))
        db.flush()
        params["state"] = state
    return f"{AUTH_URL}?{urlencode(params)}"


def consume_state(db: Session, presented: str | None) -> bool:
    """Check and burn the stored `state`. One shot: valid at most once.

    No stored state means no handshake is in flight, and that is a refusal, not
    a free pass. It used to return True in that case (to cover an upgrade with a
    handshake already open), which quietly defeated the whole check: the row is
    deleted on every callback, so from the first one onwards there was never a
    stored state to compare against and any code offered to /oauth/callback was
    accepted — exactly the "repoint the butler at someone else's account" attack
    `build_authorize_url` mints the value to prevent. A handshake that predates
    this is one click of "Connect Basecamp" away from being started again.

    `scripts/authorize.py` is unaffected: it captures the redirect on its own
    local server and never reaches this route.
    """
    row = db.get(AppState, STATE_KEY)
    expected = (row.value or "") if row else ""
    if row is not None:
        db.delete(row)
        db.flush()
    if not expected or not presented:
        return False
    return secrets.compare_digest(presented, expected)


def exchange_code(code: str) -> dict:
    """Trade an authorization code for access + refresh tokens."""
    data = {
        "type": "web_server",
        "client_id": settings.basecamp_client_id,
        "client_secret": settings.basecamp_client_secret,
        "redirect_uri": settings.basecamp_redirect_uri,
        "code": code,
    }
    # Send credentials in the form body, not the URL query string, so the client
    # secret doesn't leak into access logs / proxies (Launchpad accepts both).
    resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _refresh(refresh_token: str) -> dict:
    data = {
        "type": "refresh",
        "refresh_token": refresh_token,
        "client_id": settings.basecamp_client_id,
        "client_secret": settings.basecamp_client_secret,
    }
    resp = httpx.post(TOKEN_URL, data=data, timeout=30)
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
        # A fresh authorization (account_id is only set on the initial exchange,
        # not on refresh) may be a different Basecamp user. Drop the cached
        # identity so the next poll re-captures who "me" is — otherwise the
        # classifier keeps keying "assigned to me" / mentions off the old user.
        for key in ("my_user_id", "my_name"):
            st = db.get(AppState, key)
            if st is not None:
                db.delete(st)
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
