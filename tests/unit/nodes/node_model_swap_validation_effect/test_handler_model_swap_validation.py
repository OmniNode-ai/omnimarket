# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drive durable delivery through the handler's public protocol surface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.model_swap_validation import (
    ModelModelSwapValidationRequest,
    ModelModelSwapValidationResult,
)

from omnimarket.nodes.node_model_swap_validation_effect.handlers.handler_model_swap_validation import (
    HandlerModelSwapValidation,
    load_contract,
)
from omnimarket.nodes.node_model_swap_validation_effect.handlers.terminal_state import (
    TerminalState,
)
from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_state_config import (
    ModelModelSwapStateConfig,
)

from .test_handler_configuration import validation_request


def request() -> ModelModelSwapValidationRequest:
    return validation_request()


def completed(req: ModelModelSwapValidationRequest) -> ModelModelSwapValidationResult:
    return ModelModelSwapValidationResult(
        correlation_id=req.correlation_id,
        status="completed",
        tool_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        decision="recommend",
        recommended_model=req.candidate_model,
        lower_bound=0.95,
        judge_mode=req.judge_mode,
        quality_floor=req.quality_floor,
    )


def contract_path(tmp_path: Path, *, timeout_ms: float = 300000) -> Path:
    contract = load_contract()
    contract["state"]["directory"] = str(tmp_path / "state")
    contract["descriptor"]["timeout_ms"] = timeout_ms
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


class Validator:
    validator_id = "local-service-contract-fixture"

    def __init__(
        self, *, hold: bool = False, error: BaseException | None = None
    ) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.error = error

    async def validate(
        self, req: ModelModelSwapValidationRequest
    ) -> ModelModelSwapValidationResult:
        self.calls += 1
        self.entered.set()
        if self.error is not None:
            raise self.error
        await self.release.wait()
        return completed(req)


async def test_concurrent_duplicates_replay_one_service_result(tmp_path: Path) -> None:
    path = contract_path(tmp_path)
    provider = Validator(hold=True)
    first = HandlerModelSwapValidation(validator=provider, contract_path=path)
    second = HandlerModelSwapValidation(validator=provider, contract_path=path)
    owner = asyncio.create_task(first.handle(request()))
    await asyncio.wait_for(provider.entered.wait(), timeout=10)
    duplicate = asyncio.create_task(second.handle(request()))
    provider.release.set()
    results = await asyncio.gather(owner, duplicate)
    assert provider.calls == 1
    assert results[0].model_dump_json() == results[1].model_dump_json()
    restarted = HandlerModelSwapValidation(
        validator=Validator(error=AssertionError()), contract_path=path
    )
    assert (await restarted.handle(request())).model_dump_json() == results[
        0
    ].model_dump_json()


class ProcessCrash(BaseException):
    """Bypass normal error handling like loss of the process."""


async def test_crash_after_intent_never_calls_service_again(tmp_path: Path) -> None:
    path = contract_path(tmp_path)
    provider = Validator(error=ProcessCrash())
    with pytest.raises(ProcessCrash):
        await HandlerModelSwapValidation(validator=provider, contract_path=path).handle(
            request()
        )
    restarted = HandlerModelSwapValidation(validator=provider, contract_path=path)
    result = await restarted.handle(request())
    assert result.failure_reason == "timeout"
    assert result.tool_error_code == "interrupted_service_call"
    assert provider.calls == 1
    assert (
        await restarted.handle(request())
    ).model_dump_json() == result.model_dump_json()


async def test_crash_before_intent_can_claim_without_duplicate_send(
    tmp_path: Path,
) -> None:
    path = contract_path(tmp_path)
    state = TerminalState(
        ModelModelSwapStateConfig.model_validate(load_contract(path)["state"])
    )
    req = request()
    state.claim(req.correlation_id, state.request_hash(req.model_dump(mode="json")))
    provider = Validator()
    result = await HandlerModelSwapValidation(
        validator=provider, contract_path=path
    ).handle(req)
    assert result.status == "completed"
    assert provider.calls == 1


async def test_cancellation_persists_unknown_outcome_and_never_resends(
    tmp_path: Path,
) -> None:
    path = contract_path(tmp_path)
    provider = Validator(hold=True)
    handler = HandlerModelSwapValidation(validator=provider, contract_path=path)
    task = asyncio.create_task(handler.handle(request()))
    await asyncio.wait_for(provider.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    result = await HandlerModelSwapValidation(
        validator=provider, contract_path=path
    ).handle(request())
    assert result.failure_reason == "timeout"
    assert result.tool_error_code == "service_outcome_unknown"
    assert provider.calls == 1


async def test_service_timeout_is_durable_without_retries(tmp_path: Path) -> None:
    path = contract_path(tmp_path, timeout_ms=60000)
    provider = Validator(error=TimeoutError())
    handler = HandlerModelSwapValidation(validator=provider, contract_path=path)
    first = await handler.handle(request())
    assert first.failure_reason == "timeout"
    assert first.tool_error_code == "service_outcome_unknown"
    assert (
        await handler.handle(request())
    ).model_dump_json() == first.model_dump_json()
    assert provider.calls == 1


async def test_repository_refused_before_service_call(tmp_path: Path) -> None:
    provider = Validator()
    handler = HandlerModelSwapValidation(
        validator=provider, contract_path=contract_path(tmp_path)
    )
    result = await handler.handle(
        request().model_copy(update={"work_unit_repository": None})
    )
    assert result.failure_reason == "repository_refused"
    assert provider.calls == 0


async def test_changed_command_identity_is_rejected(tmp_path: Path) -> None:
    provider = Validator()
    handler = HandlerModelSwapValidation(
        validator=provider, contract_path=contract_path(tmp_path)
    )
    original = await handler.handle(request())
    refused = await handler.handle(
        request().model_copy(update={"task_type": "changed"})
    )
    assert refused.failure_reason == "needs_input"
    assert refused.tool_error_code == "correlation_identity_conflict"
    assert (
        await handler.handle(request())
    ).model_dump_json() == original.model_dump_json()
    assert provider.calls == 1


async def test_provider_exception_does_not_disclose_detail(tmp_path: Path) -> None:
    handler = HandlerModelSwapValidation(
        validator=Validator(error=RuntimeError("sensitive-provider-detail")),
        contract_path=contract_path(tmp_path),
    )
    result = await handler.handle(request())
    assert result.failure_reason == "tool_failed"
    assert "sensitive-provider-detail" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("correlation_id", uuid4()),
        ("quality_floor", 0.99),
        ("judge_mode", "backend"),
        ("recommended_model", "other/model"),
    ],
)
async def test_swapped_provider_cannot_change_request_binding(
    tmp_path: Path, field: str, value: object
) -> None:
    class MismatchedValidator(Validator):
        async def validate(self, req):
            return completed(req).model_copy(update={field: value})

    handler = HandlerModelSwapValidation(
        validator=MismatchedValidator(), contract_path=contract_path(tmp_path)
    )
    assert (await handler.handle(request())).failure_reason == "schema_mismatch"


def test_stale_fence_cannot_start_or_overwrite_terminal(tmp_path: Path) -> None:
    path = contract_path(tmp_path)
    state = TerminalState(
        ModelModelSwapStateConfig.model_validate(load_contract(path)["state"])
    )
    req = request()
    digest = state.request_hash(req.model_dump(mode="json"))
    first, _, _ = state.claim(req.correlation_id, digest)
    second, _, _ = state.claim(req.correlation_id, digest)
    with pytest.raises(ValueError, match="fence"):
        state.store_start_intent(req.correlation_id, first)
    state.store_terminal(req.correlation_id, second, completed(req).model_dump_json())
    with pytest.raises(ValueError, match="fence"):
        state.store_terminal(req.correlation_id, second, "replacement")


async def test_lock_path_failure_is_a_typed_refusal_without_host_paths(
    tmp_path: Path,
) -> None:
    path = contract_path(tmp_path)
    state_path = tmp_path / "state"
    state_path.mkdir()
    (state_path / f"{request().correlation_id}.lock").mkdir()
    provider = Validator()
    result = await HandlerModelSwapValidation(
        validator=provider, contract_path=path
    ).handle(request())
    assert result.failure_reason == "needs_input"
    assert result.tool_error_code == "state_unavailable"
    assert str(tmp_path) not in result.model_dump_json()
    assert provider.calls == 0


async def test_state_corruption_after_discovery_does_not_resend(tmp_path: Path) -> None:
    path = contract_path(tmp_path)
    provider = Validator()
    handler = HandlerModelSwapValidation(validator=provider, contract_path=path)
    await handler.handle(request())
    (tmp_path / "state" / "terminal.sqlite3").write_bytes(b"not sqlite")
    result = await handler.handle(request())
    assert result.failure_reason == "needs_input"
    assert result.tool_error_code == "state_unavailable"
    assert str(tmp_path) not in result.model_dump_json()
    assert provider.calls == 1


@pytest.mark.parametrize(
    "damage", ["malformed_json", "invalid_schema", "wrong_correlation", "invalid_fence"]
)
async def test_corrupt_state_records_fail_closed_without_resend(
    tmp_path: Path, damage: str
) -> None:
    import sqlite3

    path = contract_path(tmp_path)
    provider = Validator()
    handler = HandlerModelSwapValidation(validator=provider, contract_path=path)
    original = await handler.handle(request())
    with sqlite3.connect(tmp_path / "state" / "terminal.sqlite3") as database:
        if damage == "invalid_fence":
            database.execute("UPDATE validation_runs SET fence = ?", ("corrupt",))
        else:
            payload = {
                "malformed_json": "{",
                "invalid_schema": "{}",
                "wrong_correlation": original.model_copy(
                    update={"correlation_id": uuid4()}
                ).model_dump_json(),
            }[damage]
            database.execute("UPDATE validation_runs SET terminal = ?", (payload,))
    refused = await handler.handle(request())
    assert refused.failure_reason == "needs_input"
    assert refused.tool_error_code == "state_unavailable"
    assert str(tmp_path) not in refused.model_dump_json()
    assert provider.calls == 1
