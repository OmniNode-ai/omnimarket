# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input models for node_entropy_experiment_orchestrator (OMN-13614).

The orchestrator is deterministic and does NO I/O. Per-framework track metrics
(success, cost, latency, coverage, failure classes) are supplied by the caller in
fixture/replay mode -- the same replay-proven discipline as
node_on_vs_off_experiment_compute. Running the tracks (LLM delegation, coverage
subprocess) is an EFFECT concern handled upstream; this orchestrator only
aggregates pre-captured track evidence into the canonical
``ModelExperimentResult`` (omnibase_core, OMN-13613).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_failure import (
    EntropyFailureClass,
)

__all__ = [
    "ModelEntropyExperimentRequest",
    "ModelEntropyTrackInput",
]


class ModelEntropyTrackInput(BaseModel):
    """Pre-captured evidence for a single framework track of the experiment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    track_id: str = Field(
        ...,
        min_length=1,
        description="Stable per-track identity, e.g. 'omninode:0'.",
    )
    framework: str = Field(
        ...,
        min_length=1,
        description="Framework under test for this track (e.g. 'omninode').",
    )
    succeeded: bool = Field(
        ...,
        description="Whether the track produced a valid, contract-passing artifact.",
    )
    total_cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="Total dollar cost of this track (Decimal, never negative).",
    )
    latency_ms: int = Field(
        default=0,
        ge=0,
        description="Wall-clock latency of the track in milliseconds.",
    )
    lines_of_code: int = Field(
        default=0,
        ge=0,
        description="Non-blank, non-comment lines of code produced by the track.",
    )
    test_coverage_pct: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Generated-code test coverage percentage, if measured.",
    )
    failure_classes: tuple[EntropyFailureClass, ...] = Field(
        default_factory=tuple,
        description="Closed failure classification(s) for a failed track.",
    )


class ModelEntropyExperimentRequest(BaseModel):
    """Aggregation request: identifiers plus the set of completed framework tracks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: UUID = Field(
        ...,
        description="Unique identifier for this experiment.",
    )
    run_id: UUID = Field(
        ...,
        description="Identifier of the specific run that produced these tracks.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation identifier linking events across the run.",
    )
    runtime_identity: str = Field(
        ...,
        min_length=1,
        description="Runtime lane/service identity that executed the experiment.",
    )
    evidence_id: UUID = Field(
        ...,
        description="Canonical UUID of the durable evidence record for this run.",
    )
    artifact_ref: str | None = Field(  # content-address-ok: sha256-prefixed digest
        default=None,
        description=(
            "Optional content-addressed artifact reference "
            "('sha256:<64 lowercase hex chars>') for the raw evidence bytes."
        ),
    )
    tracks: tuple[ModelEntropyTrackInput, ...] = Field(
        ...,
        min_length=1,
        description="Completed framework tracks to aggregate (at least one).",
    )
