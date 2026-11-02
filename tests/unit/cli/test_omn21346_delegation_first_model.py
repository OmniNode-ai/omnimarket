# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A developer chooses the model delegation tries first, after onboarding (OMN-21346).

``onex models`` let a developer order the models of one key, but which provider
a run started on was not theirs to choose: local models first, then whatever
key the ladder reached. ``onex models first PROVIDER [MODEL]`` names the one
model every run tries first, across providers. ``onex models list`` shows it
first, and the next delegation's first attempt is that model. Fakes only: the
provider's model list, the pinned test delegation and the model call are stubs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.delegation_first_choice import first_choice_route
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_models,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.local_byok_route import registered_local_model_routes
from tests.unit.cli.test_model_setup_local_url_omn21162 import server

pytestmark = pytest.mark.unit

# The fixture this module shares with the OMN-21162 tests.
__all__ = ["server"]

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_KEY = "sk-or-v1-synthetic-omn21346-key-never-printed"
_A = "openai/gpt-5-nano"
_B = "google/gemma-4-31b-it:free"
_LOCAL = "qwen3-coder-30b"
_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "delegation.sqlite"
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )

    def answered(backend_id: str) -> tuple[int, str, str]:
        receipt = tmp_path / f"run-{uuid4().hex}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": _LOCAL,
                    "endpoint": "stub",
                }
            ),
            encoding="utf-8",
        )
        return 0, "hello\n", f"receipt: {receipt}\n"

    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", answered)

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in (_A, _B)]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    return state_dir


def _models(*args: str) -> Any:
    return CliRunner().invoke(models_group, list(args))


def _rows() -> list[dict[str, Any]]:
    shown = _models("list", "--json")
    assert shown.exit_code == 0, shown.output
    return [json.loads(line) for line in shown.output.splitlines()]


def _openrouter_key_with(*models: str) -> None:
    stored = CliRunner().invoke(
        secret_group,
        ["set", "llm.openrouter.api_key", "--model", models[0]],
        input=_KEY,
    )
    assert stored.exit_code == 0, stored.output
    for model in models[1:]:
        added = _models("add-model", "openrouter", "--model", model)
        assert added.exit_code == 0, added.output


def _local_model(server: str) -> None:
    added = _models("add", "local", "--url", server, "--model", _LOCAL)
    assert added.exit_code == 0, added.output


def test_first_sets_the_model_and_list_shows_it_first(server: str) -> None:
    _local_model(server)
    _openrouter_key_with(_A, _B)

    chosen = _models("first", "openrouter", _B)
    assert chosen.exit_code == 0, chosen.output
    assert f"Delegation now tries OpenRouter {_B} first" in chosen.output

    rows = _rows()
    assert rows[0]["provider"] == "openrouter"
    assert rows[0]["tried_first"] is True
    assert rows[0]["models"] == [_B, _A]
    assert all(row["tried_first"] is False for row in rows[1:])
    assert resolve_local_byok_credential_models("openrouter") == (_B, _A)

    text = _models("list").output.splitlines()
    assert text[0].startswith("OpenRouter")
    assert f"tried first: {_B}" in text[0]


def test_first_without_a_model_keeps_the_keys_own_first(server: str) -> None:
    _local_model(server)
    _openrouter_key_with(_A, _B)
    chosen = _models("first", "openrouter")
    assert chosen.exit_code == 0, chosen.output
    rows = _rows()
    assert (rows[0]["provider"], rows[0]["models"]) == ("openrouter", [_A, _B])


def test_first_on_a_local_model(server: str) -> None:
    _openrouter_key_with(_A)
    _local_model(server)
    _models("first", "openrouter")
    chosen = _models("first", "local", _LOCAL)
    assert chosen.exit_code == 0, chosen.output
    rows = _rows()
    assert rows[0]["provider"] == "local"
    assert rows[0]["tried_first"] is True
    assert [row["provider"] for row in rows if row["tried_first"]] == ["local"]


def test_first_refuses_a_model_not_in_the_list_and_changes_nothing() -> None:
    _openrouter_key_with(_A, _B)
    refused = _models("first", "openrouter", "not/a-model")
    assert refused.exit_code != 0
    assert "not in your openrouter list" in refused.output
    assert resolve_local_byok_credential_models("openrouter") == (_A, _B)
    assert not any(row["tried_first"] for row in _rows())


def test_first_refuses_a_provider_not_set_up() -> None:
    refused = _models("first", "gemini")
    assert refused.exit_code != 0
    assert "onex models add gemini" in refused.output


def test_clear_puts_the_default_order_back(server: str) -> None:
    _openrouter_key_with(_A)
    _local_model(server)
    assert _models("first", "openrouter").exit_code == 0
    cleared = _models("first", "--clear")
    assert cleared.exit_code == 0, cleared.output
    assert not any(row["tried_first"] for row in _rows())
    assert first_choice_route("research") is None


def test_a_removed_key_is_no_longer_tried_first(server: str) -> None:
    _openrouter_key_with(_A)
    _local_model(server)
    assert _models("first", "openrouter").exit_code == 0
    assert first_choice_route("research") is not None
    assert _models("remove", "openrouter").exit_code == 0
    assert first_choice_route("research") is None
    assert not any(row["tried_first"] for row in _rows())


class _Effect:
    """Records each call; every model answers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_id)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_ANSWER,
            tokens_in=11,
            tokens_out=22,
        )


class _FirstFails(_Effect):
    """The chosen model is unavailable; the run moves on to the local model."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        if request.model_id != _B:
            return super().__call__(request)
        self.calls.append(request.model_id)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            content="",
            tokens_in=0,
            tokens_out=0,
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            http_status=503,
            error_message="upstream unavailable",
        )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path, effect: _Effect
) -> dict[str, Any]:
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(state / "bifrost_overrides.yaml"))

    # The run's ladder would start on the local model (no key route needed).
    def ladder(
        self: LocalDelegationDispatchPort, task_type: str, **_: object
    ) -> ModelResolvedDelegationBackend:
        del self
        return registered_local_model_routes(task_type)[0]

    monkeypatch.setattr(LocalDelegationDispatchPort, "_resolve_initial_backend", ladder)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


def test_the_next_delegation_tries_the_chosen_model_first(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_A, _B)
    assert _models("first", "openrouter", _B).exit_code == 0

    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, state, effect)

    assert effect.calls[0] == _B
    first = result["attempts"][0]
    assert (first["backend_id"], first["model_id"]) == ("byok-openrouter", _B)
    assert result["status"] == "completed"


def test_a_failed_first_choice_moves_on_to_the_usual_order(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_A, _B)
    assert _models("first", "openrouter", _B).exit_code == 0

    effect = _FirstFails()
    result = _dispatch(monkeypatch, tmp_path, state, effect)

    # The key's own retry budget may ask the chosen model again before moving on.
    assert effect.calls[0] == _B
    assert effect.calls[-1] == _LOCAL
    assert set(effect.calls[:-1]) == {_B}
    assert result["attempts"][0]["model_id"] == _B
    assert result["attempts"][-1]["model_id"] == _LOCAL
    assert result["status"] == "completed"


def test_without_a_choice_the_local_model_is_still_tried_first(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_A, _B)

    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, state, effect)

    assert effect.calls[0] == _LOCAL
    assert result["attempts"][0]["backend_id"] == "local-url"
