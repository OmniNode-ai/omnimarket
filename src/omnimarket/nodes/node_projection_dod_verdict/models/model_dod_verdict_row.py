# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One durable definition-of-done verification run (OMN-18900).

One row per RUN, not per ticket. The metric this table exists to serve is
work-caused attempts until the definition of done verifies true, which is a
question about a ticket's history: a table keyed on the ticket alone would
hold only the last answer and could never be asked how many attempts preceded
it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_projection_dod_verdict.models.enum_dod_eval_outcome import (
    EnumDodEvalOutcome,
)
from omnimarket.nodes.node_projection_dod_verdict.models.enum_dod_eval_refusal import (
    EnumDodEvalRefusal,
)


class ModelDodVerdictRow(BaseModel):
    """A single verification run, with its class counts and its eval outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1, description="Linear ticket.")
    correlation_id: UUID = Field(..., description="The run's correlation id.")
    completed_at: datetime = Field(
        ..., description="Run end, event time. Third component of the key."
    )
    started_at: datetime = Field(..., description="Run start, event time.")

    status: EnumDodVerifyStatus = Field(..., description="Terminal status.")
    unresolved_cause: EnumDodVerifyUnresolvedCause | None = Field(
        default=None, description="Set only when the status is unresolved."
    )

    total_checks: int = Field(..., ge=0)
    verified_count: int = Field(..., ge=0)
    failed_count: int = Field(..., ge=0)
    skipped_count: int = Field(..., ge=0)
    superseded_count: int = Field(..., ge=0)
    non_probative_count: int = Field(..., ge=0)
    behavior_proving_count: int = Field(..., ge=0)
    readback_proving_count: int = Field(..., ge=0)
    unbindable_overlay_count: int = Field(..., ge=0)

    outcome: EnumDodEvalOutcome = Field(
        ...,
        description=(
            "The done predicate's answer AT PROJECTION TIME, stored so a "
            "reader does not have to re-derive it and so a later change to "
            "the rule is visible as a disagreement rather than as a silent "
            "re-scoring of history. The counts are on the row beside it, so "
            "the current rule is always re-runnable over a stored row."
        ),
    )
    outcome_refusal: EnumDodEvalRefusal | None = Field(
        default=None, description="Which conjunct failed. Set only when refused."
    )

    error_message: str = Field(
        default="",
        description="Failure detail, empty string rather than null so a reader "
        "never distinguishes absent from unset.",
    )


__all__ = ["ModelDodVerdictRow"]
