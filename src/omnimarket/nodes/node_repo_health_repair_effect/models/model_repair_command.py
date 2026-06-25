# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input command model for node_repo_health_repair_effect.

Carries the REPO_BASELINE classification result and the baseline evidence
needed to create a durable repair task (Linear ticket under OMN-13316).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.repo_health import ModelRepoHealthClassification


class ModelRepoHealthRepairCommand(BaseModel):
    """Command to emit a durable repair task for a REPO_BASELINE failure.

    The handler creates (or idempotently references an existing) Linear ticket
    under the parent epic, keyed on the content hash of the failing command and
    failing path set so repeated sweep iterations do not create duplicates.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID threading this command through the lane."
    )
    classification: ModelRepoHealthClassification = Field(
        ...,
        description=(
            "The REPO_BASELINE classification from node_repo_health_classify_compute "
            "that triggered this repair dispatch."
        ),
    )
    parent_issue_id: str = Field(
        default="OMN-13316",
        description="Linear parent epic to attach the repair task to.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, compute and return the content key without emitting a "
            "Linear ticket. Useful for idempotency proofs in tests."
        ),
    )


__all__ = ["ModelRepoHealthRepairCommand"]
