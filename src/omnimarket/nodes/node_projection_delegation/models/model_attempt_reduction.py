# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure attempt-ladder reduction for delegation terminals (OMN-15503).

The delegation terminal event carries a *declared* outcome (``status``,
``quality_gate_passed``) and a typed *attempt ladder* (``attempts``). Before
OMN-15503 the projection trusted the declared outcome and dropped the ladder,
so a command whose every tier was refused with HTTP 429 could still land a
durable row saying the delegation succeeded — the 2026-07-29 matrix row
``refactor | FAIL | Google Gemini HTTP 429 after two escalation attempts``
projected as an outer-completed success with no machine-readable cause.

This module reduces the ladder to a typed outcome. It is a pure function over
already-validated typed inputs: no I/O, no clock, no provider contact. The
caller (``HandlerProjectionDelegation.project_delegate_skill_terminal``) folds
the result onto the ``delegation_events`` row.

Placement: projection-private to node_projection_delegation, mirroring
node_generation_consumer's ``model_attempt_reduction``. Not promoted to
omnibase_core — only this node consumes it (per the A5 layer rule). The
*cause* vocabulary is NOT redefined here: it is
``omnibase_core``'s ``EnumDelegationTerminalFailureCause`` (0.46.8), the same
enum the producer-side ``ModelDelegationResult.terminal_failure_cause`` uses,
so the two sides of the seam share one typed vocabulary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)

# Typed transport failure classes that mean "the provider refused for capacity
# reasons". Matched first, before any text heuristic, so a producer that
# already speaks the typed vocabulary is never re-classified by string search.
QUOTA_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED.value,
        "quota_exhausted",
        "resource_exhausted",
        "rate_limited",
        "rate_limit_exceeded",
        "insufficient_quota",
    }
)

# Text fallback for producers that predate the typed failure_class (the Kafka
# bus dispatch path reports free-text error messages only). Deliberately
# narrow: an HTTP 429 status token, or the two canonical provider quota
# phrasings. A generic "error" or "failed" string never reaches this branch.
_QUOTA_TEXT_PATTERN = re.compile(
    r"(?:\b429\b|RESOURCE_EXHAUSTED|quota exceeded|insufficient[_ ]quota"
    r"|rate limit exceeded)",
    re.IGNORECASE,
)


def _attempt_is_quota_refusal(attempt: ModelDelegateSkillAttemptRecord) -> bool:
    """True when this attempt was refused by the provider for capacity."""
    failure_class = (attempt.failure_class or "").strip().lower()
    if failure_class:
        return failure_class in QUOTA_FAILURE_CLASSES
    return bool(_QUOTA_TEXT_PATTERN.search(attempt.error_message or ""))


def _attempt_succeeded(attempt: ModelDelegateSkillAttemptRecord) -> bool:
    """True when this attempt actually produced an accepted answer.

    An attempt that carries any ``failure_class`` never counts as a success,
    regardless of its ``quality_gate_passed`` flag — the flag defaults to
    False on skipped tiers but a malformed producer could set both.
    """
    return attempt.quality_gate_passed and not (attempt.failure_class or "").strip()


class ModelDelegationAttemptReduction(BaseModel):
    """Typed outcome reduced from a delegation's attempt ladder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    terminal_ok: bool = Field(
        description=(
            "Authoritative outer outcome. False whenever the ladder proves no "
            "attempt produced an accepted answer, EVEN IF the terminal event "
            "declared status='completed' (OMN-15503 defect 1)."
        ),
    )
    terminal_failure_cause: EnumDelegationTerminalFailureCause | None = Field(
        default=None,
        description=(
            "Machine-readable cause when the ladder proves a typed terminal "
            "failure. None when the delegation succeeded or when the failure "
            "has no typed cause in the current vocabulary — None is NOT a "
            "claim of success; read terminal_ok for that."
        ),
    )
    attempt_history: tuple[ModelDelegateSkillAttemptRecord, ...] = Field(
        default=(),
        description=(
            "The typed ladder, in order, preserved across the projection "
            "boundary so 'refused after N escalations' is provable from the "
            "durable row rather than from a capture log."
        ),
    )
    quota_refusal_count: int = Field(
        default=0,
        ge=0,
        description="Number of attempts refused by the provider for capacity.",
    )

    @property
    def has_typed_failure(self) -> bool:
        """True when a typed cause was resolved (used by the sticky fold)."""
        return self.terminal_failure_cause is not None


def reduce_delegation_attempts(
    *,
    declared_status: str,
    declared_quality_gate_passed: bool,
    error_message: str = "",
    attempts: Iterable[ModelDelegateSkillAttemptRecord] = (),
) -> ModelDelegationAttemptReduction:
    """Reduce a delegation terminal's attempt ladder to a typed outcome.

    Precedence (deliberate, and the whole point of OMN-15503):

    1. The **ladder** decides. If every attempt is a provider capacity
       refusal and none succeeded, the outcome is
       ``terminal_ok=False`` + ``PROVIDER_QUOTA_EXHAUSTED`` — no matter what
       ``declared_status`` says.
    2. Only when the ladder is empty (bus dispatch paths that do not report
       per-attempt detail) does the declared status decide ``terminal_ok``;
       a quota signal in ``error_message`` can still type the cause.

    Pure: no I/O, no clock, no network.
    """
    ladder = tuple(attempts)
    quota_refusals = tuple(a for a in ladder if _attempt_is_quota_refusal(a))
    any_success = any(_attempt_succeeded(a) for a in ladder)

    if ladder:
        # 1. Ladder-authoritative branch.
        if any_success:
            return ModelDelegationAttemptReduction(
                terminal_ok=declared_status == "completed"
                and declared_quality_gate_passed,
                terminal_failure_cause=None,
                attempt_history=ladder,
                quota_refusal_count=len(quota_refusals),
            )
        cause = (
            EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
            if quota_refusals
            else None
        )
        return ModelDelegationAttemptReduction(
            terminal_ok=False,
            terminal_failure_cause=cause,
            attempt_history=ladder,
            quota_refusal_count=len(quota_refusals),
        )

    # 2. Ladder-less branch — declared status decides ok; text can type cause.
    declared_ok = declared_status == "completed" and declared_quality_gate_passed
    cause = (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
        if not declared_ok and _QUOTA_TEXT_PATTERN.search(error_message or "")
        else None
    )
    return ModelDelegationAttemptReduction(
        terminal_ok=declared_ok,
        terminal_failure_cause=cause,
        attempt_history=(),
        quota_refusal_count=0,
    )


__all__ = [
    "QUOTA_FAILURE_CLASSES",
    "ModelDelegationAttemptReduction",
    "reduce_delegation_attempts",
]
