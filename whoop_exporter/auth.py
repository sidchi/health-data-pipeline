"""WHOOP OAuth 2.0 helpers shared by the CLI and the Cloud Function.

WHOOP issues short-lived access tokens and *rotates* the refresh token on every
use: after a refresh, the old refresh token is dead and the new one must be
persisted before anything else can fail. Callers are responsible for storage;
this module only talks to the token endpoint.
"""

from __future__ import annotations

import time
from typing import Any

import requests

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"  # noqa: S105 (URL, not a secret)
SCOPES = (
    "read:recovery read:sleep read:workout read:cycles read:body_measurement read:profile offline"
)
DEFAULT_TIMEOUT_SECONDS = 30
# Treat a token as expired this many seconds early to absorb clock skew.
EXPIRY_SKEW_SECONDS = 300

TokenResponse = dict[str, Any]


class TokenError(RuntimeError):
    """The token endpoint rejected the request (e.g. revoked refresh token)."""


def _post_token(
    data: dict[str, str],
    session: requests.Session | None,
    clock: float | None,
) -> TokenResponse:
    http = session or requests.Session()
    resp = http.post(TOKEN_URL, data=data, timeout=DEFAULT_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        # Never echo the request body: it contains the client secret.
        raise TokenError(f"WHOOP token endpoint returned HTTP {resp.status_code}")
    token: TokenResponse = resp.json()
    if "access_token" not in token:
        raise TokenError("WHOOP token response did not include an access_token")
    token["obtained_at"] = time.time() if clock is None else clock
    return token


def refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    *,
    session: requests.Session | None = None,
    clock: float | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a new access token (and rotated refresh token)."""
    return _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        session,
        clock,
    )


def exchange_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    *,
    session: requests.Session | None = None,
    clock: float | None = None,
) -> TokenResponse:
    """Exchange an authorization code from the browser redirect for tokens."""
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        session,
        clock,
    )


def is_access_token_fresh(token: TokenResponse, now: float | None = None) -> bool:
    """True if ``token`` has an access token that won't expire within the skew window."""
    if not token.get("access_token"):
        return False
    now = time.time() if now is None else now
    expires_in = float(token.get("expires_in", 3600))
    obtained_at = float(token.get("obtained_at", 0))
    return now < obtained_at + expires_in - EXPIRY_SKEW_SECONDS
