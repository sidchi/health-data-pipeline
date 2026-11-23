from types import SimpleNamespace

from whoop_exporter.config import Settings
from whoop_exporter.gcp import SecretManagerRefreshTokenStore, get_access_token

from .fakes import FakeResponse, FakeSession

SETTINGS = Settings(whoop_client_id="cid", whoop_client_secret="cs", gdrive_folder_id="f")


class FakeSecretClient:
    def __init__(self, value: str) -> None:
        self.value = value
        self.accessed: list[str] = []
        self.added: list[dict] = []

    def access_secret_version(self, request):
        self.accessed.append(request["name"])
        return SimpleNamespace(payload=SimpleNamespace(data=f"{self.value}\n".encode()))

    def add_secret_version(self, request):
        self.added.append(request)


def test_store_reads_latest_version_and_strips_whitespace():
    fake = FakeSecretClient("r1")
    store = SecretManagerRefreshTokenStore("proj", "whoop-refresh-token", client=fake)
    assert store.load() == "r1"
    assert fake.accessed == ["projects/proj/secrets/whoop-refresh-token/versions/latest"]


def test_rotated_refresh_token_is_persisted():
    fake = FakeSecretClient("r1")
    store = SecretManagerRefreshTokenStore("proj", "s", client=fake)
    session = FakeSession(
        {"/oauth/oauth2/token": FakeResponse({"access_token": "a2", "refresh_token": "r2"})}
    )

    assert get_access_token(SETTINGS, store, session=session) == "a2"
    assert session.calls[0][2]["data"]["refresh_token"] == "r1"
    assert fake.added == [{"parent": "projects/proj/secrets/s", "payload": {"data": b"r2"}}]


def test_no_new_version_when_refresh_token_not_rotated():
    fake = FakeSecretClient("r1")
    store = SecretManagerRefreshTokenStore("proj", "s", client=fake)
    session = FakeSession({"/oauth/oauth2/token": FakeResponse({"access_token": "a2"})})
    get_access_token(SETTINGS, store, session=session)
    assert fake.added == []
