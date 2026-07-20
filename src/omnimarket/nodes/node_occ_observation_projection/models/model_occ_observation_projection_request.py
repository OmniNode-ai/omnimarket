# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationProjectionRequest — the raw observation trail input (OMN-14851).

The storage-agnostic input to the dedup projection: an arbitrary (unordered,
possibly duplicate-containing) collection of append-only raw observation
records. Pure input, zero I/O — how the caller sourced these records (which
durable store, if any) is out of scope for this contract.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_observation_record import ModelOccObservationRecord


class ModelOccObservationProjectionRequest(BaseModel):
    """Input to the OCC observation dedup projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelOccObservationRecord, ...] = Field(
        default=(),
        description=(
            "The raw append-only observation records (any order, may contain "
            "duplicate attempts or multiple reruns of the same source tuple)."
        ),
    )


__all__ = ["ModelOccObservationProjectionRequest"]
