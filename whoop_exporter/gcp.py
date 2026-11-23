"""Google Cloud integrations used by the Cloud Function entry point."""

from __future__ import annotations

from typing import Any

from .auth import refresh_access_token
from .config import Settings

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


class SecretManagerRefreshTokenStore:
    """Stores the (rotating) WHOOP refresh token as Secret Manager versions."""

    def __init__(self, project_id: str, secret_id: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
        self._client = client
        self._secret_path = f"projects/{project_id}/secrets/{secret_id}"

    def load(self) -> str:
        response = self._client.access_secret_version(
            request={"name": f"{self._secret_path}/versions/latest"}
        )
        return response.payload.data.decode("utf-8").strip()

    def save(self, refresh_token: str) -> None:
        self._client.add_secret_version(
            request={
                "parent": self._secret_path,
                "payload": {"data": refresh_token.encode("utf-8")},
            }
        )


def get_access_token(
    settings: Settings, store: SecretManagerRefreshTokenStore, **kwargs: Any
) -> str:
    """Refresh the WHOOP access token, persisting the rotated refresh token first."""
    token = refresh_access_token(
        store.load(), settings.whoop_client_id, settings.whoop_client_secret, **kwargs
    )
    if token.get("refresh_token"):
        store.save(token["refresh_token"])
    return token["access_token"]


def default_project_id() -> str:
    import google.auth

    _, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError("Could not determine the GCP project id from default credentials")
    return project_id


def drive_service_from_default_credentials() -> Any:
    """Drive v3 client authenticated as the runtime service account."""
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)
