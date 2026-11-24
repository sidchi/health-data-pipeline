"""Static checks that keep infra/terraform in step with the Python code.

``terraform validate`` (run in CI) proves the configuration is well-formed for
the provider. These tests cover what it cannot: that the function Terraform
deploys is configured the way ``whoop_exporter.config`` expects, and that a few
security invariants hold (no secret values or service-account keys in state, no
public invoker).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2
import pytest

import main
from whoop_exporter import config

TF_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform"
CI_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _unquote(value: Any) -> Any:
    """python-hcl2 keeps the quotes on string literals and block labels; drop them."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _normalize(node: Any) -> Any:
    if isinstance(node, dict):
        return {_unquote(k): _normalize(v) for k, v in node.items() if k != "__is_block__"}
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    return _unquote(node)


@pytest.fixture(scope="module")
def tf() -> dict[str, Any]:
    """All .tf files merged into {"resource": {type: {name: body}}, "variable": {...}, ...}."""
    merged: dict[str, dict[str, Any]] = {}
    for path in sorted(TF_DIR.glob("*.tf")):
        with path.open(encoding="utf-8") as fh:
            doc = _normalize(hcl2.load(fh))
        for block_type, entries in doc.items():
            target = merged.setdefault(block_type, {})
            for entry in entries:
                for key, body in entry.items():
                    if block_type == "resource":
                        for name, resource in body.items():
                            target.setdefault(key, {})[name] = resource
                    else:
                        target[key] = body
    return merged


@pytest.fixture(scope="module")
def tf_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TF_DIR.glob("*.tf")))


@pytest.fixture(scope="module")
def function(tf: dict[str, Any]) -> dict[str, Any]:
    return tf["resource"]["google_cloudfunctions2_function"]["export"]


def _service_config(function: dict[str, Any]) -> dict[str, Any]:
    return function["service_config"][0]


def test_every_file_parses_and_declares_the_core_resources(tf: dict[str, Any]) -> None:
    resources = tf["resource"]
    for kind in (
        "google_cloudfunctions2_function",
        "google_cloud_scheduler_job",
        "google_secret_manager_secret",
        "google_service_account",
        "google_cloud_run_service_iam_member",
    ):
        assert kind in resources, kind


def test_entry_point_exists_in_main(function: dict[str, Any]) -> None:
    entry_point = function["build_config"][0]["entry_point"]
    assert callable(getattr(main, entry_point, None))


def test_runtime_is_a_python_version_ci_tests(function: dict[str, Any]) -> None:
    runtime = function["build_config"][0]["runtime"]
    match = re.fullmatch(r"python3(\d+)", runtime)
    assert match, runtime
    assert f'"3.{match.group(1)}"' in CI_WORKFLOW.read_text(encoding="utf-8")


def test_function_env_satisfies_load_settings(function: dict[str, Any]) -> None:
    """The env Terraform sets must be exactly what the Cloud Function path requires."""
    service = _service_config(function)
    plain = set(service["environment_variables"])
    secret = {block["key"] for block in service["secret_environment_variables"]}
    assert not plain & secret, "a variable is set both as plain env and as a secret"

    env = {key: "x" for key in plain | secret}
    env["DAYS_BACK"] = "7"
    env["WHOOP_SECRET_ID"] = "custom-secret"
    settings = config.load_settings(env, Path("/nonexistent.json"), require_drive_folder=True)
    assert settings.whoop_secret_id == "custom-secret"

    # Every required key is set, and credentials only ever arrive as secrets.
    for missing in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET", "GDRIVE_FOLDER_ID"):
        partial = {k: v for k, v in env.items() if k != missing}
        with pytest.raises(config.ConfigError, match=missing):
            config.load_settings(partial, Path("/nonexistent.json"), require_drive_folder=True)
    assert {"WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"} <= secret


def test_refresh_token_secret_wiring(tf: dict[str, Any], function: dict[str, Any]) -> None:
    secret_ids = tf["variable"]["secret_ids"]["default"]
    assert secret_ids["refresh_token"] == config.DEFAULT_WHOOP_SECRET_ID

    env = _service_config(function)["environment_variables"]
    assert 'this["refresh_token"]' in env["WHOOP_SECRET_ID"]

    # The function may add versions (token rotation) to the refresh-token secret only.
    adder = tf["resource"]["google_secret_manager_secret_iam_member"]["runtime_version_adder"]
    assert adder["role"] == "roles/secretmanager.secretVersionAdder"
    assert 'this["refresh_token"]' in adder["secret_id"]


def test_days_back_validation_matches_the_function(tf: dict[str, Any]) -> None:
    condition = tf["variable"]["days_back"]["validation"][0]["condition"]
    assert ">= 1" in condition and "<= 365" in condition
    assert tf["variable"]["days_back"]["default"] == config.DEFAULT_DAYS_BACK
    creds = {"WHOOP_CLIENT_ID": "a", "WHOOP_CLIENT_SECRET": "b"}
    no_file = Path("/nonexistent.json")
    assert config.load_settings({**creds, "DAYS_BACK": "365"}, no_file).days_back == 365
    with pytest.raises(config.ConfigError):
        config.load_settings({**creds, "DAYS_BACK": "366"}, no_file)


def test_single_instance_prevents_concurrent_token_rotation(function: dict[str, Any]) -> None:
    service = _service_config(function)
    assert service["max_instance_count"] == 1
    assert service["max_instance_request_concurrency"] == 1


def test_function_runs_as_dedicated_accounts(function: dict[str, Any]) -> None:
    assert "google_service_account.runtime" in _service_config(function)["service_account_email"]
    assert "google_service_account.build" in function["build_config"][0]["service_account"]


def test_scheduler_authenticates_and_retries(tf: dict[str, Any]) -> None:
    job = tf["resource"]["google_cloud_scheduler_job"]["daily"]
    target = job["http_target"][0]
    assert "google_service_account.invoker" in target["oidc_token"][0]["service_account_email"]
    assert 1 <= job["retry_config"][0]["retry_count"] <= 5  # Cloud Scheduler's limit

    invoker = tf["resource"]["google_cloud_run_service_iam_member"]["invoker"]
    assert invoker["role"] == "roles/run.invoker"
    assert "google_service_account.invoker" in invoker["member"]


@pytest.mark.parametrize(
    "forbidden",
    [
        # Secret values would sit in plain text in the state file.
        "google_secret_manager_secret_version",
        # Long-lived keys are what Workload Identity Federation replaces.
        "google_service_account_key",
        # The function must never be publicly invokable.
        "allUsers",
        "allAuthenticatedUsers",
    ],
)
def test_security_invariants(tf_text: str, forbidden: str) -> None:
    assert forbidden not in tf_text


def test_workload_identity_is_pinned_to_one_repository(tf: dict[str, Any]) -> None:
    provider = tf["resource"]["google_iam_workload_identity_pool_provider"]["github"]
    assert "assertion.repository ==" in provider["attribute_condition"]
    assert provider["oidc"][0]["issuer_uri"] == "https://token.actions.githubusercontent.com"
