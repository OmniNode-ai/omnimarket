# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's request, result, snapshot and events (OMN-22921)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import ModelLandingDecision, ModelLandingFacts
from omnimarket.models.landing_tick import (
    ModelTickDivergence,
    ModelTickSubjectRow,
)


class EnumTickMode(StrEnum):
    """How a tick runs. Replay and observe never write; act needs the overlay to enable it."""

    REPLAY = "replay"
    OBSERVE = "observe"
    ACT = "act"


class EnumTickStatus(StrEnum):
    COMPARED = "compared"
    FAILED = "failed"


class EnumTickFailureReason(StrEnum):
    ACT_NOT_ENABLED = "act_not_enabled"
    RECORDED_DECISION_MISSING = "recorded_decision_missing"
    DECISION_RAISED = "decision_raised"


class ModelTickSnapshot(BaseModel):
    """One recorded tick: the facts read, and the script's decision from them when one was recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelLandingFacts
    decision: ModelLandingDecision | None = Field(
        default=None,
        description=(
            "The script's decision for the tick; None for a landing snapshot step record, "
            "which carries facts only (the tick then ends recorded_decision_missing)."
        ),
    )
    fair_share: bool = Field(
        default=True,
        description="Whether the script shared free slots across repositories.",
    )
    provenance: str = Field(default="", description="Where the snapshot came from.")


class ModelTickRequest(BaseModel):
    """Run one landing tick through the node path and compare it with the recorded decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: EnumTickMode
    facts: ModelLandingFacts
    recorded_decision: ModelLandingDecision | None = None
    fair_share: bool = True


class ModelTickResult(BaseModel):
    """The tick's comparison, or its typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: EnumTickMode
    status: EnumTickStatus
    tick: int
    equal: bool
    rows: tuple[ModelTickSubjectRow, ...] = ()
    divergences: tuple[ModelTickDivergence, ...] = ()
    node_actions: int = 0
    recorded_actions: int | None = None
    writes_performed: int = Field(
        default=0,
        description="Merges, arms and reruns the tick performed. Observe and replay: 0.",
    )
    recorded_written: bool = Field(
        default=False,
        description=(
            "Act mode wrote the recorded decision itself: the cutover repository is one the old "
            "controller rules out of scope, so its recorded decision is no action, and the "
            "comparison covers that repository's subjects only (OMN-23458)."
        ),
    )
    failure_reason: EnumTickFailureReason | None = None
    failure_detail: str = ""


class ModelTickComparedEvent(BaseModel):
    """Emitted when a tick was compared: equal, or the pull requests that differ."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: EnumTickMode
    tick: int
    equal: bool
    divergent_subjects: tuple[str, ...]
    node_actions: int
    recorded_actions: int | None
    writes_performed: int


class ModelTickFailedEvent(BaseModel):
    """Emitted when a tick could not be compared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: EnumTickMode
    tick: int
    reason: EnumTickFailureReason
    detail: str


__all__: list[str] = [
    "EnumTickFailureReason",
    "EnumTickMode",
    "EnumTickStatus",
    "ModelTickComparedEvent",
    "ModelTickFailedEvent",
    "ModelTickRequest",
    "ModelTickResult",
    "ModelTickSnapshot",
]
