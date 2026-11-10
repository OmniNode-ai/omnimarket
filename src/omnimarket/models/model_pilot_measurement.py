# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the piece 16 pilot measurement: a pilot's declared pass bar against what the pilot reported (OMN-23611).

Shared by node_pilot_measurement_compute (the pure aggregation) and node_pilot_measurement_effect (the only writer of the
piece 16 reading row), so neither imports the other's package. The judgement of one pilot is node_pilot_manifest_check_compute's
(OMN-22979): a bar frozen before the start whose hash matches, then the results held against the bar. This model adds what that
node does not hold and the reading needs: the pilot's result deadline, which bars gate the reading, and the facts a pilot
reported. The plan's reading row for piece 16 (section 3.5) fails when a pilot starts without a frozen bar, when a planted fact
is not recalled, or when an eval run ends with no result. A pilot that misses a bar that does not gate the reading is rejected
as a pilot (plan 7.2) and the reading still reads WORKING.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)


class ModelPilotDeclaration(BaseModel):
    """What the overlay declares of one pilot before its experiment: its pass bar, which bars gate the reading and its deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pilot_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    bar: tuple[ModelPilotBarCriterion, ...] = Field(min_length=1)
    #: Metrics of ``bar`` whose miss makes the reading NOT_WORKING (a planted fact not recalled). A miss of any other bar only
    #: rejects the pilot.
    gating_metrics: tuple[str, ...] = Field(min_length=1)
    #: A started pilot with no result this many seconds after it started has ended with no result.
    result_deadline_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelPilotDeclaration:
        metrics = [criterion.metric for criterion in self.bar]
        if len(set(self.gating_metrics)) != len(self.gating_metrics):
            raise ValueError(f"pilot {self.pilot_id}: a gating metric is named twice")
        unknown = sorted(set(self.gating_metrics) - set(metrics))
        if unknown:
            raise ValueError(
                f"pilot {self.pilot_id}: gating metrics {unknown} are not in its bar"
            )
        return self


class ModelPilotFact(BaseModel):
    """What one started pilot reported: the bar hash it froze (None when none), when it froze it and started, and its result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pilot_id: str = Field(min_length=1)
    bar_hash: str | None = None
    frozen_at: AwareDatetime | None = None
    started_at: AwareDatetime
    #: None while the pilot has no result; with a result, when it finished and what it measured.
    finished_at: AwareDatetime | None = None
    results: dict[str, float] | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelPilotFact:
        if (self.finished_at is None) != (self.results is None):
            raise ValueError("finished_at and results come together or not at all")
        return self


class ModelPilotMeasurementRequest(BaseModel):
    """Everything the reading is a function of; it reads no clock, file or network."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    declarations: tuple[ModelPilotDeclaration, ...]
    facts: tuple[ModelPilotFact, ...] = ()
    #: Why the pilots' facts could not be read; the reading is then UNKNOWN, never an empty answer.
    facts_unread_reason: str | None = None


class ModelPilotMeasurementResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    #: Token-safe reason codes (no spaces or pipes), in declaration order.
    reasons: tuple[str, ...]
    pilots_declared: int = Field(ge=0)
    pilots_started: int = Field(ge=0)
    pilots_resulted: int = Field(ge=0)
    #: Pilots that started and have no result yet, inside their deadline.
    pending: tuple[str, ...] = ()
    #: Pilots the manifest check rejected on a bar that does not gate the reading: no fault of the reading (plan 7.2).
    rejected: tuple[str, ...] = ()
    #: Pilots the manifest check accepted: every bar met.
    accepted: tuple[str, ...] = ()


__all__ = [
    "ModelPilotDeclaration",
    "ModelPilotFact",
    "ModelPilotMeasurementRequest",
    "ModelPilotMeasurementResult",
]
