# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC3: a failed row whose only rung was refused for a missing credential names a cause.

The lab dev lane's ``delegation_events`` held failed rows written after the
pre-dispatch fix with a NULL ``terminal_failure_cause``: one rung, backend
``openrouter-nemotron-ultra``, ``failure_class`` ``provider_credential_missing``,
``acceptance_reason`` ``provider_call_failed``, and an ``error_message`` that
reads "Delegation refused: credential_absent ...". The gate judged nothing and
no rung hit a capacity refusal, so ``reduce_delegation_attempts`` derived no
cause from the ladder, although the delegate-skill producer resolves the same
ladder to ``provider_error`` (``resolve_terminal_failure_cause``).

The projection falls back to that producer rule when the terminal declared no
cause and the existing gate and capacity readings found none. A declared cause
still wins unchanged, and a ladder that records nothing stays unclassified.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    ModelDelegationAttemptReduction,
    reduce_delegation_attempts,
)

pytestmark = pytest.mark.unit

_REFUSAL_TEXT = (
    "Delegation refused: credential_absent for llm.openrouter.api_key on backend "
    "'https://openrouter.ai/api/v1/chat/completions'."
)


def _credential_missing_rung() -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="cheap_frontier",
        backend_id="openrouter-nemotron-ultra",
        model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
        quality_gate_passed=False,
        failure_class="provider_credential_missing",
        error_message=_REFUSAL_TEXT,
        acceptance_decision=EnumDelegationAcceptanceDecision.TERMINATE,
        acceptance_reason=EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED,
    )


def _reduce(
    attempts: tuple[ModelDelegateSkillAttemptRecord, ...],
    *,
    declared: EnumDelegationTerminalFailureCause | None = None,
    error_message: str = "",
) -> ModelDelegationAttemptReduction:
    return reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message=error_message,
        attempts=attempts,
        declared_failure_cause=declared,
    )


def test_a_credential_refused_rung_names_provider_error_when_the_terminal_names_nothing() -> (
    None
):
    result = _reduce((_credential_missing_rung(),))

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )
    assert result.terminal_ok is False


def test_a_rung_with_a_typed_auth_failure_names_auth_failed() -> None:
    rejected = _credential_missing_rung().model_copy(
        update={"failure_class": "provider_auth_failed", "error_message": ""}
    )

    result = _reduce((rejected,))

    assert (
        result.terminal_failure_cause is EnumDelegationTerminalFailureCause.AUTH_FAILED
    )


def test_a_declared_cause_still_wins_over_the_credential_fallback() -> None:
    result = _reduce(
        (_credential_missing_rung(),),
        declared=EnumDelegationTerminalFailureCause.TIMEOUT,
    )

    assert result.terminal_failure_cause is EnumDelegationTerminalFailureCause.TIMEOUT


def test_a_failed_ladder_that_records_nothing_stays_unclassified() -> None:
    # Negative control: silence is not a provider error.
    silent = _credential_missing_rung().model_copy(
        update={"failure_class": None, "error_message": ""}
    )

    result = _reduce((silent,))

    assert result.terminal_failure_cause is None
    assert result.terminal_ok is False


def test_a_capacity_refusal_keeps_the_quota_cause() -> None:
    # Negative control: the existing capacity reading is not overridden.
    limited = _credential_missing_rung().model_copy(
        update={"failure_class": "rate_limited", "error_message": ""}
    )

    result = _reduce((limited,))

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )
