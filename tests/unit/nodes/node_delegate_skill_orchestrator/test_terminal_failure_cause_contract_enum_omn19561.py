# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19561 AC4: terminal failure causes stay within the declared contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    resolve_terminal_failure_cause,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)


@pytest.fixture
def contract_enum() -> set[str]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return set(contract["outputs"]["terminal_failure_cause"]["enum"])


def _attempt(
    *,
    error_message: str = "",
    failure_class: str | None = None,
    acceptance_decision: EnumDelegationAcceptanceDecision | None = None,
    acceptance_reason: EnumDelegationAcceptanceReason | None = None,
) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="backend-under-test",
        model_id="model-under-test",
        quality_gate_passed=False,
        error_message=error_message,
        failure_class=failure_class,
        acceptance_decision=acceptance_decision,
        acceptance_reason=acceptance_reason,
    )


def test_contract_enum_equals_core_terminal_failure_causes(
    contract_enum: set[str],
) -> None:
    assert contract_enum == {
        cause.value for cause in EnumDelegationTerminalFailureCause
    }


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        pytest.param(
            [_attempt(error_message='HTTP 429 {"error": "RESOURCE_EXHAUSTED"}')],
            EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED,
            id="quota-429-body",
        ),
        pytest.param(
            [_attempt(error_message="HTTP 401 Unauthorized")],
            EnumDelegationTerminalFailureCause.AUTH_FAILED,
            id="auth-401",
        ),
        pytest.param(
            [
                _attempt(
                    acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
                    acceptance_reason=(
                        EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR
                    ),
                )
            ],
            EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
            id="gate-refusal",
        ),
        pytest.param(
            [_attempt(failure_class="timeout")],
            EnumDelegationTerminalFailureCause.TIMEOUT,
            id="timeout-failure-class",
        ),
        pytest.param(
            [_attempt(error_message="unrecognised upstream failure")],
            EnumDelegationTerminalFailureCause.PROVIDER_ERROR,
            id="unrecognised-error-text",
        ),
        pytest.param(
            [
                _attempt(
                    acceptance_decision=EnumDelegationAcceptanceDecision.CLIMB,
                    acceptance_reason=(
                        EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR
                    ),
                ),
                _attempt(error_message="HTTP 429 quota exceeded"),
            ],
            EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
            id="gate-refusal-then-quota",
        ),
    ],
)
def test_resolved_terminal_failure_cause_is_in_contract_enum(
    contract_enum: set[str],
    attempts: list[ModelDelegateSkillAttemptRecord],
    expected: EnumDelegationTerminalFailureCause,
) -> None:
    cause = resolve_terminal_failure_cause(attempts)

    assert cause is not None
    assert cause.value in contract_enum
    assert cause is expected
