import pytest

from whoop_exporter import auth

from .fakes import FakeResponse, FakeSession


def token_route(payload=None, status=200):
    return FakeSession({"/oauth/oauth2/token": FakeResponse(payload, status)})


def test_refresh_posts_refresh_grant_and_stamps_obtained_at():
    session = token_route({"access_token": "a2", "refresh_token": "r2", "expires_in": 3600})
    token = auth.refresh_access_token("r1", "cid", "csecret", session=session, clock=1000.0)

    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", auth.TOKEN_URL)
    assert kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": "cid",
        "client_secret": "csecret",
    }
    assert kwargs["timeout"] == auth.DEFAULT_TIMEOUT_SECONDS
    assert token["access_token"] == "a2"
    assert token["obtained_at"] == 1000.0


def test_exchange_code_posts_authorization_code_grant():
    session = token_route({"access_token": "a"})
    auth.exchange_code("the-code", "http://localhost/cb", "cid", "cs", session=session)
    data = session.calls[0][2]["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["redirect_uri"] == "http://localhost/cb"


def test_rejected_refresh_raises_without_leaking_secret():
    session = token_route({"error": "invalid_grant"}, status=400)
    with pytest.raises(auth.TokenError) as exc_info:
        auth.refresh_access_token("r1", "cid", "super-secret", session=session)
    assert "400" in str(exc_info.value)
    assert "super-secret" not in str(exc_info.value)


def test_response_without_access_token_raises():
    with pytest.raises(auth.TokenError, match="access_token"):
        auth.refresh_access_token("r", "c", "s", session=token_route({"refresh_token": "x"}))


@pytest.mark.parametrize(
    ("now", "fresh"),
    [(1000.0, True), (1000.0 + 3600 - 301, True), (1000.0 + 3600 - 299, False)],
)
def test_freshness_applies_skew(now, fresh):
    token = {"access_token": "a", "expires_in": 3600, "obtained_at": 1000.0}
    assert auth.is_access_token_fresh(token, now=now) is fresh


def test_token_without_access_token_is_not_fresh():
    assert auth.is_access_token_fresh({"expires_in": 3600, "obtained_at": 0}, now=0) is False
