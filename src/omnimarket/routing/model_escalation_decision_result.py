# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared result model for the delegation escalation-decision COMPUTE (OMN-13476).

Lives in the shared ``omnimarket.routing`` package so both the COMPUTE and the
delegating orchestrator import it without a cross-node reach-in.

The deterministic verdict: either escalate to ``next_tier_name`` or terminate
with a ``terminal_failure_reason``. Exactly one of the two is populated —
``can_escalate`` True implies ``next_tier_name`` set and ``terminal_failure_reason``
None; ``can_escalate`` False implies the inverse. The model enforces this with a
post-init validator so a malformed verdict cannot be constructed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelEscalationDecisionResult(BaseModel):
    """Deterministic escalate-or-terminate verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    can_escalate: bool = Field(
        ...,
        description="Whether escalation to a higher tier should proceed.",
    )
    next_tier_name: str | None = Field(
        default=None,
        description=(
            "The tier to escalate to. Set iff ``can_escalate`` is True; the "
            "value is the orchestrator-supplied ``next_tier_name``."
        ),
    )
    terminal_failure_reason: str | None = Field(
        default=None,
        description=(
            "Machine-keyable reason escalation cannot proceed. Set iff "
            "``can_escalate`` is False."
        ),
    )

    @model_validator(mode="after")
    def _verdict_is_consistent(self) -> ModelEscalationDecisionResult:
        if self.can_escalate:
            if self.next_tier_name is None:
                raise ValueError("can_escalate=True requires next_tier_name to be set")
            if self.terminal_failure_reason is not None:
                raise ValueError(
                    "can_escalate=True must not carry a terminal_failure_reason"
                )
        else:
            if self.terminal_failure_reason is None:
                raise ValueError(
                    "can_escalate=False requires terminal_failure_reason to be set"
                )
            if self.next_tier_name is not None:
                raise ValueError("can_escalate=False must not carry next_tier_name")
        return self


__all__ = ["ModelEscalationDecisionResult"]
