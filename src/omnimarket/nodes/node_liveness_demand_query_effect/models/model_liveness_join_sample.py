# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLivenessJoinSample — one representative correlated-join outcome.

OMN-15126 implementation of the OMN-14845 design (design §3.2, §5). This is
the raw evidence node_liveness_demand_query_effect hands to
node_liveness_evaluate_compute so the latter can populate the exact-join
fields on a `ModelLivenessReceipt` (omnibase_core) without performing any I/O
itself. Two independent samples may be produced by one query cycle: at most
one representative item whose join succeeded (`expected_value_predicate_result
is True`, used to populate a HEALTHY receipt) and at most one representative
item whose join failed (used to populate a RED receipt's "None-with-reason"
evidence per design §5).
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.models.runtime.model_event_ref import ModelEventRef
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelLivenessJoinSample"]


class ModelLivenessJoinSample(BaseModel):
    """One representative eligible-demand item's correlated-join outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    input_event_ref: ModelEventRef
    terminal_event_ref: ModelEventRef | None = Field(
        default=None,
        description="Set only when a matching terminal-topic row was found.",
    )
    projection_key_canonical: str | None = Field(default=None)
    projection_value_hash: str | None = Field(default=None)
    projection_expected_value_hash: str | None = Field(default=None)
    expected_value_predicate_result: bool = Field(
        ...,
        description="True iff a matching row was found AND the declared "
        "expected_value_predicate held against it.",
    )
