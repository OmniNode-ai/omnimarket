# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A provider 429 is not a verdict on the key (OMN-21464).

The clean-VM walk of 2026-10-27 saw an OpenRouter free model answer 429 and
the key then listed as failed, so delegation stopped using a valid key. The
test delegation is a stub that prints what ``onex delegate --json`` prints; the
429 body is the recorded OpenRouter text. No network is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    local_byok_key_test_failed,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    _was_throttled,
)
from omnimarket.routing import byok_model_discovery

pytestmark = pytest.mark.unit

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_KEY = "sk-or-v1-synthetic-omn21464-key-never-printed"
_MODEL = "google/gemma-4-31b-it:free"
_RECORDED_429 = f"{_MODEL} is temporarily rate-limited upstream. Please retry shortly"
_CRED = "cred_localinstall_openrouter_0123456789abcdef"


def _delegate_output(
    failure_class: str, message: str, *, refusal: bool = False, cause: str = ""
) -> tuple[int, str, str]:
    """What a failed pinned ``onex delegate --json`` run prints."""
    result: dict[str, Any] = {
        "attempts": [
            {
                "backend_id": "byok-openrouter",
                "failure_class": failure_class,
                "error_message": message,
            }
        ]
    }
    if refusal:
        result["credential_refusal"] = {"reason": "credential_rejected"}
    prefix = f"{cause}: " if cause else ""
    return (
        1,
        json.dumps({"result": result}) + "\n",
        f"onex delegate failed: {prefix}pinned_backend_failed: {message}\n",
    )


def _throttled_run(_backend_id: str) -> tuple[int, str, str]:
    return _delegate_output(
        "rate_limited",
        f"Client error '429 Too Many Requests' | provider response: {_RECORDED_429}",
    )


def _rejected_run(_backend_id: str) -> tuple[int, str, str]:
    return _delegate_output(
        "provider_auth_failed",
        f"Delegation refused: credential_rejected for {_CRED}",
        refusal=True,
        cause="auth_failed",
    )


def _passing_run(tmp_path: Path) -> Any:
    def run(backend_id: str) -> tuple[int, str, str]:
        path = tmp_path / "receipt.json"
        path.write_text(
            json.dumps({"status": "success", "backend_id": backend_id, "model": _MODEL})
        )
        return (0, "", f"receipt: {path.as_posix()}")

    return run


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        byok_model_discovery,
        "get_models_json",
        lambda **_: {"data": [{"id": _MODEL}]},
    )
    return db_path


def _run_as(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", run)


def _add_passing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _run_as(monkeypatch, _passing_run(tmp_path))
    added = CliRunner().invoke(
        models_group, ["add", "openrouter", "--model", _MODEL], input=_KEY
    )
    assert added.exit_code == 0, added.output


def _listed() -> str:
    return CliRunner().invoke(models_group, ["list"]).output


def test_ac1_a_429_is_a_retryable_throttle_never_credential_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_passing(monkeypatch, tmp_path)
    _run_as(monkeypatch, _throttled_run)
    result = CliRunner().invoke(models_group, ["test", "openrouter", "--json"])
    shown = json.loads(result.output.strip().splitlines()[-1])
    assert shown["status"] == "throttled", result.output
    assert "credential_rejected" not in result.output
    assert _RECORDED_429 in shown["reason"]
    assert _was_throttled(*_throttled_run("b")[1:]) is True


def test_ac2_a_429_leaves_the_stored_verdict_and_the_key_stays_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_passing(monkeypatch, tmp_path)
    before = _listed()
    assert "last test passed" in before
    _run_as(monkeypatch, _throttled_run)
    CliRunner().invoke(models_group, ["test", "openrouter"])
    assert _listed() == before
    assert local_byok_key_test_failed("openrouter") is False


def test_ac2_a_429_on_a_never_tested_key_does_not_mark_it_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_as(monkeypatch, _throttled_run)
    added = CliRunner().invoke(
        models_group, ["add", "openrouter", "--model", _MODEL], input=_KEY
    )
    assert "Delegation does not use this key" not in added.output
    assert local_byok_key_test_failed("openrouter") is False
    assert "FAILED" not in _listed()


def test_ac2_the_same_key_after_a_401_is_marked_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_passing(monkeypatch, tmp_path)
    _run_as(monkeypatch, _throttled_run)
    CliRunner().invoke(models_group, ["test", "openrouter"])
    assert local_byok_key_test_failed("openrouter") is False
    _run_as(monkeypatch, _rejected_run)
    CliRunner().invoke(models_group, ["test", "openrouter"])
    assert local_byok_key_test_failed("openrouter") is True
    assert "last test FAILED" in _listed()


def test_ac3_a_401_still_reports_credential_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_passing(monkeypatch, tmp_path)
    _run_as(monkeypatch, _rejected_run)
    result = CliRunner().invoke(models_group, ["test", "openrouter", "--json"])
    shown = json.loads(result.output.strip().splitlines()[-1])
    assert shown["status"] == "failed"
    assert "credential_rejected" in shown["reason"]
    assert _was_throttled(*_rejected_run("b")[1:]) is False


def test_the_dispatch_classifier_reads_429_as_retryable_and_401_403_as_rejection() -> (
    None
):
    from omnimarket.enums.enum_delegation_failure_class import (
        EnumDelegationFailureClass as Failure,
    )
    from omnimarket.inference.provider_response_error import failure_class_for_status
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        _is_retryable_transport_failure,
    )

    throttled = failure_class_for_status(429, _RECORDED_429)
    assert throttled is Failure.RATE_LIMITED
    assert _is_retryable_transport_failure(throttled) is True
    for status in (401, 403):
        rejected = failure_class_for_status(status, "Invalid credentials")
        assert rejected is Failure.PROVIDER_AUTH_FAILED
        assert _is_retryable_transport_failure(rejected) is False
