# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationProjectionResult — the deduplicated projection output (OMN-14851).

Exactly one deterministic representative observation per distinct exact source
tuple, ready to feed
``ModelOccAutoauthorWindowRequest.observations`` unchanged (no change to the
window counter's own semantics).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation


class ModelOccObservationProjectionResult(BaseModel):
    """The deduplicated qualifying-observation projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelOccAutoauthorObservation, ...] = Field(
        default=(),
        description=(
            "One deterministic representative observation per distinct exact "
            "source tuple (product_repo, product_pr_number, head_sha, "
            "policy_version) — valid input to "
            "ModelOccAutoauthorWindowRequest.observations."
        ),
    )
    total_raw_records: int = Field(
        ...,
        ge=0,
        description="Count of input records before dedup (post raw-key dedup).",
    )
    distinct_source_tuples: int = Field(
        ...,
        ge=0,
        description="Count of distinct exact source tuples in the projection (== len(observations)).",
    )


__all__ = ["ModelOccObservationProjectionResult"]
