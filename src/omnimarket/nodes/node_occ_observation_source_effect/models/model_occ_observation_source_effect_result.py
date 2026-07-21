# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationSourceEffectResult — the OCC-backed dedup projection output (OMN-14888)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation


class ModelOccObservationSourceEffectResult(BaseModel):
    """The deduplicated qualifying-observation projection, read from OCC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelOccAutoauthorObservation, ...] = Field(
        default=(),
        description="One deterministic representative observation per distinct "
        "exact source tuple — valid input to "
        "ModelOccAutoauthorWindowRequest.observations, unchanged.",
    )
    raw_record_count: int = Field(
        ..., ge=0, description="Count of raw record files parsed from the checkout."
    )
    distinct_source_tuples: int = Field(
        ...,
        ge=0,
        description="Count of distinct exact source tuples (== len(observations)).",
    )
    malformed_paths: tuple[str, ...] = Field(
        default=(),
        description="Repo-relative paths under drift/occ_observations/ that failed "
        "to parse as a ModelOccObservationRecord (fail-closed: never silently "
        "dropped from the count without being surfaced here).",
    )


__all__ = ["ModelOccObservationSourceEffectResult"]
