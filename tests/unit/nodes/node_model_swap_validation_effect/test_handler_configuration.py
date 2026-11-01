# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime discovery must not require environment or persistent state."""

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.model_swap_validation import ModelModelSwapValidationRequest

from omnimarket.nodes.node_model_swap_validation_effect.handlers.handler_model_swap_validation import (
    HandlerModelSwapValidation,
    load_contract,
)


def configured_contract(tmp_path: Path, *, directory: str = "") -> Path:
    contract = load_contract()
    contract["state"]["directory"] = directory
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def validation_request() -> ModelModelSwapValidationRequest:
    return ModelModelSwapValidationRequest(
        correlation_id="11111111-1111-4111-8111-111111111111",
        task_type="public-example",
        reference_model="reference/large",
        candidate_model="candidate/small",
        judge_mode="stub",
        answer_source="recorded",
        replay_set_digest="a" * 64,
        quality_floor=0.9,
        max_cost_usd=1.0,
        work_unit_repository="example/public",
    )


def test_default_handler_is_discoverable_without_environment(monkeypatch) -> None:
    monkeypatch.delenv("ONEX_MODEL_SWAP_STATE_DIRECTORY", raising=False)
    HandlerModelSwapValidation()


def test_constructor_does_not_create_contract_or_environment_state(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "contract-state"
    env_state = tmp_path / "environment-state"
    monkeypatch.setenv("ONEX_MODEL_SWAP_STATE_DIRECTORY", str(env_state))
    path = configured_contract(tmp_path, directory=str(state))
    HandlerModelSwapValidation(contract_path=path)
    assert not state.exists()
    assert not env_state.exists()


@pytest.mark.asyncio
async def test_missing_state_refuses_only_on_first_call(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ONEX_MODEL_SWAP_STATE_DIRECTORY", raising=False)
    handler = HandlerModelSwapValidation(contract_path=configured_contract(tmp_path))
    result = await handler.handle(validation_request())
    assert result.status == "failed"
    assert result.failure_reason == "needs_input"
    assert result.tool_error_code == "configuration_invalid"
    assert result.evidence_only is True


@pytest.mark.asyncio
async def test_invalid_yaml_is_a_first_call_refusal(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("state: [unterminated", encoding="utf-8")
    handler = HandlerModelSwapValidation(contract_path=path)
    result = await handler.handle(validation_request())
    assert result.failure_reason == "needs_input"
    assert result.tool_error_code == "configuration_invalid"


@pytest.mark.asyncio
async def test_corrupt_state_is_a_typed_first_call_refusal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "terminal.sqlite3").write_bytes(b"not sqlite")
    handler = HandlerModelSwapValidation(
        contract_path=configured_contract(tmp_path, directory=str(state))
    )
    result = await handler.handle(validation_request())
    assert result.failure_reason == "needs_input"
    assert result.tool_error_code == "configuration_invalid"
