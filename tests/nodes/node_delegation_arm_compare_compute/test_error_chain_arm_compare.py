# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 of OMN-20017, error chain: a malformed request ends as a typed failure, never a verdict."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    default_command_topic,
    default_response_topic,
)
from omnimarket.models.delegation_arm_compare.enum_arm_compare_failure import (
    EnumArmCompareFailure,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_result import (
    ModelArmCompareResult,
)
from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import (
    PASS,
    failure_result,
    overlay,
    planted,
    request,
)

pytestmark = pytest.mark.unit

NAME = "node_delegation_arm_compare_compute"
OVERLAY_ROOT = Path(__file__).resolve().parent / "fixtures" / "overlays"


def _handle(req: ModelArmCompareRequest) -> ModelArmCompareResult:
    return HandlerDelegationArmCompare(overlay=overlay()).handle(req)


def _assert_failure(answer: ModelArmCompareResult, code: EnumArmCompareFailure) -> None:
    assert answer.failure is code
    assert answer.result is None
    assert answer.control is None
    assert answer.detail


def test_error_chain_same_arm_is_a_typed_failure() -> None:
    a, b = planted(both_pass=90, both_fail=0, a_only=0, b_only=0)
    req = request(a, b).model_copy(update={"arm_b_model": "model-a"})
    _assert_failure(_handle(req), EnumArmCompareFailure.SAME_ARM)


def test_error_chain_repeated_task_key_is_a_typed_failure() -> None:
    a, b = planted(both_pass=90, both_fail=0, a_only=0, b_only=0)
    twice = (*a, ModelArmTaskOutcome(task_key=a[0].task_key, outcome=PASS))
    answer = _handle(request(twice, b))
    _assert_failure(answer, EnumArmCompareFailure.DUPLICATE_TASK)
    assert a[0].task_key in answer.detail


def test_error_chain_no_tasks_is_a_typed_failure() -> None:
    _assert_failure(_handle(request((), ())), EnumArmCompareFailure.NO_TASKS)


def test_error_chain_missing_overlay_is_a_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    a, b = planted(both_pass=90, both_fail=0, a_only=0, b_only=0)
    _assert_failure(
        HandlerDelegationArmCompare().handle(request(a, b)),
        EnumArmCompareFailure.OVERLAY_UNAVAILABLE,
    )


def test_error_chain_a_result_holding_a_verdict_and_a_failure_is_refused() -> None:
    a, b = planted(both_pass=90, both_fail=0, a_only=0, b_only=0)
    good = _handle(request(a, b))
    assert good.result is not None
    with pytest.raises(ValidationError):
        ModelArmCompareResult(
            comparison_id="x",
            result=good.result,
            failure=EnumArmCompareFailure.NO_TASKS,
        )
    with pytest.raises(ValidationError):
        ModelArmCompareResult(comparison_id="x")
    assert failure_result("x", EnumArmCompareFailure.NO_TASKS).result is None


def test_error_chain_an_outcome_value_outside_the_enum_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelArmTaskOutcome(task_key="t", outcome="maybe")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_error_chain_malformed_payload_ends_failed_with_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(), state_root=tmp_path
    )
    terminal, _ = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name=NAME,
            requester="arm-compare-error-chain",
            payload={"comparison_id": "c", "outcomes_a": []},
            correlation_id=uuid4(),
            response_topic=default_response_topic(),
            timeout_seconds=10,
        )
    )
    assert terminal.status == "failed"
    assert terminal.payload is None
    assert "ModelArmCompareRequest" in (terminal.error_message or "")
