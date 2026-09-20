# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input to the pure fingerprint derivation (def-B request)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runtime_error_fingerprints.models.model_runtime_error_event_wire import (
    ModelRuntimeErrorEventWire,
)


class ModelRuntimeErrorFingerprintRequest(BaseModel):
    """One runtime-error event plus the database facts the derivation needs.

    The prior state is HANDED IN rather than looked up inside the handler: the
    handler is pure and deterministic, and the accumulated count is a database
    fact, not a property of the event. Replaying the same request therefore
    reproduces the same row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelRuntimeErrorEventWire = Field(..., description="The arriving event.")

    prior_occurrence_count: int = Field(
        default=0,
        ge=0,
        description="Occurrences already stored for this fingerprint, or 0.",
    )
    prior_first_seen_at: datetime | None = Field(
        default=None,
        description="Earliest occurrence already stored, or None on first sight.",
    )


__all__ = ["ModelRuntimeErrorFingerprintRequest"]
