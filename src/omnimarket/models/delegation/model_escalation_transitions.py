# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared retry, move, skip and terminate rules of a task class.

Every task class in ``task_class_contracts.v1.yaml`` declares four lists of
causes under ``transitions``. A failed or skipped rung is classified into one
cause from the closed vocabulary below. The escalation decision node looks the
cause up in the class's lists, so the transition a cause yields is a property of
the contract and the node holds no rule of its own about which cause moves.

The four lists partition the vocabulary: every cause is declared exactly once,
and a class with any list missing refuses to load. Four causes have a fixed
home because the decision node raises them itself: ``retry_budget_spent`` is a
move, and ``pinned_backend_failed``, ``escalation_limit`` and
``ladder_exhausted`` terminate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumEscalationCause(StrEnum):
    """Why a rung did not produce an accepted answer."""

    # retry: another attempt on the same rung.
    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"
    # move: the run continues on a different rung.
    RETRY_BUDGET_SPENT = "retry_budget_spent"
    RATE_LIMITED = "rate_limited"
    RUNG_TIMEOUT = "rung_timeout"
    QUALITY_GATE_REFUSED = "quality_gate_refused"
    RUBRIC_FAILED = "rubric_failed"
    # skip: a rung not attempted.
    CREDENTIAL_WITHHELD = "credential_withheld"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_REVOKED = "credential_revoked"
    CREDENTIAL_EXPIRED = "credential_expired"
    CREDENTIAL_BILLING_BLOCKED = "credential_billing_blocked"
    MODEL_NOT_FOUND = "model_not_found"
    QUOTA_COOLDOWN = "quota_cooldown"
    QUOTA_WINDOW_FULL = "quota_window_full"
    QUOTA_STATE_UNKNOWN = "quota_state_unknown"
    CONTEXT_TOO_LARGE = "context_too_large"
    BUDGET_EXHAUSTED = "budget_exhausted"
    COST_CLASS_NOT_ALLOWED = "cost_class_not_allowed"
    BEYOND_CLASS_LIMIT = "beyond_class_limit"
    RUNG_NOT_REACHABLE_FROM_RUNTIME = "rung_not_reachable_from_runtime"
    # terminate: no later provider call.
    PINNED_BACKEND_FAILED = "pinned_backend_failed"
    QUALITY_VETO = "quality_veto"
    ESCALATION_LIMIT = "escalation_limit"
    LADDER_EXHAUSTED = "ladder_exhausted"
    NON_RETRYABLE_FAILURE = "non_retryable_failure"


class EnumTransitionKind(StrEnum):
    """What a run does after a rung that produced no accepted answer."""

    RETRY = "retry"
    MOVE = "move"
    SKIP = "skip"
    TERMINATE = "terminate"


class EnumMoveKind(StrEnum):
    """How a move reaches its next rung."""

    MODEL_MOVE = "model_move"
    KEY_MOVE = "key_move"
    SIBLING_FALLBACK = "sibling_fallback"
    TIER_CLIMB = "tier_climb"


# The causes the decision node raises itself, and the list each must sit in.
_FIXED_HOMES: dict[EnumEscalationCause, EnumTransitionKind] = {
    EnumEscalationCause.RETRY_BUDGET_SPENT: EnumTransitionKind.MOVE,
    EnumEscalationCause.PINNED_BACKEND_FAILED: EnumTransitionKind.TERMINATE,
    EnumEscalationCause.ESCALATION_LIMIT: EnumTransitionKind.TERMINATE,
    EnumEscalationCause.LADDER_EXHAUSTED: EnumTransitionKind.TERMINATE,
}


class ModelTaskClassTransitions(BaseModel):
    """One task class's four transition lists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retry_on: tuple[EnumEscalationCause, ...] = Field(...)
    move_on: tuple[EnumEscalationCause, ...] = Field(...)
    skip_on: tuple[EnumEscalationCause, ...] = Field(...)
    terminate_on: tuple[EnumEscalationCause, ...] = Field(...)

    @model_validator(mode="after")
    def _lists_partition_the_vocabulary(self) -> Self:
        declared = [
            *self.retry_on,
            *self.move_on,
            *self.skip_on,
            *self.terminate_on,
        ]
        repeated = sorted({c.value for c in declared if declared.count(c) > 1})
        if repeated:
            raise ValueError(f"transition causes declared more than once: {repeated}")
        missing = sorted(c.value for c in EnumEscalationCause if c not in declared)
        if missing:
            raise ValueError(f"transition causes declared in no list: {missing}")
        misplaced = sorted(
            cause.value
            for cause, kind in _FIXED_HOMES.items()
            if self.kind_of(cause) is not kind
        )
        if misplaced:
            raise ValueError(
                "retry_budget_spent must be in move_on and pinned_backend_failed, "
                f"escalation_limit and ladder_exhausted in terminate_on: {misplaced}"
            )
        return self

    def kind_of(self, cause: EnumEscalationCause) -> EnumTransitionKind:
        """The list ``cause`` is declared in."""
        if cause in self.retry_on:
            return EnumTransitionKind.RETRY
        if cause in self.move_on:
            return EnumTransitionKind.MOVE
        if cause in self.skip_on:
            return EnumTransitionKind.SKIP
        return EnumTransitionKind.TERMINATE


__all__ = [
    "EnumEscalationCause",
    "EnumMoveKind",
    "EnumTransitionKind",
    "ModelTaskClassTransitions",
]
