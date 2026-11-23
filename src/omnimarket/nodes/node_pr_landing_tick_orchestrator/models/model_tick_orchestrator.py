# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's request, result, snapshot and events (OMN-22921)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.running_read_due import (
    ModelMergedPrFact,
)
from omnimarket.models.landing_controller_carry import ModelLandingControllerDecisions
from omnimarket.models.landing_decision import (
    EnumLivenessRead,
    ModelLandingDecision,
    ModelLandingFacts,
)
from omnimarket.models.landing_tick import (
    ModelTickDivergence,
    ModelTickSubjectRow,
)
from omnimarket.models.pr_landing_rerun_request.model_landing_rerun_answer import (
    ModelLandingRerunAnswer,
)
from omnimarket.models.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_running_read import (
    ModelTickRunningRead,
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
    CONTROLLER_TICK_MISSING = "controller_tick_missing"
    TICK_STAGE_RAISED = "tick_stage_raised"


class EnumTickSkipReason(StrEnum):
    """Why the snapshot source skipped one record instead of reading it."""

    UNPARSEABLE_RECORD = "unparseable_record"


class ModelTickSkippedRecord(BaseModel):
    """One record the snapshot source could not parse: skipped and named, so the rest of the read stands (OMN-23529).

    The record topic is shared, so a record a newer or older producer shape wrote (a field this reader does
    not know) is skipped with this row instead of failing the whole read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=0, description="The record's place in the read, from 0.")
    reason: EnumTickSkipReason
    detail: str = Field(
        description="The first validation errors as location: error type, so the shape that differs is named."
    )


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
    controller: ModelLandingControllerDecisions | None = Field(
        default=None,
        description=(
            "The old controller's finished tick the record's state was carried from, with its "
            "decision rows only, never its facts; None outside the cutover period (OMN-23665)."
        ),
    )
    merged_prs: tuple[ModelMergedPrFact, ...] = Field(
        default=(),
        description="Pull requests merged since the last tick, carried to the tick request for the running-read due row (OMN-26549).",
    )
    running_read_ledger_text: str | None = Field(
        default=None,
        description="The ledger text the tick reads due rows from, carried to the tick request (OMN-26549).",
    )


class ModelTickRequest(BaseModel):
    """Run one landing tick through the node path and compare it with the recorded decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: EnumTickMode
    facts: ModelLandingFacts
    recorded_decision: ModelLandingDecision | None = None
    fair_share: bool = True
    controller: ModelLandingControllerDecisions | None = Field(
        default=None,
        description=(
            "The old controller's finished tick carried on the record; when set, the "
            "comparison runs against that controller's decision (OMN-23665)."
        ),
    )
    rerun_requests: tuple[ModelLandingRerunRequest, ...] = Field(
        default=(),
        description="The open rerun requests lanes made: facts of this tick, each answered once (OMN-23830).",
    )
    merged_prs: tuple[ModelMergedPrFact, ...] = Field(
        default=(),
        description=(
            "Pull requests merged since the last tick, as the landing controller read them at merge: each "
            "runtime-affecting one is mapped to its running-read due row (OMN-26549)."
        ),
    )
    running_read_ledger_text: str | None = Field(
        default=None,
        description=(
            "The ledger text the due rows are read from at the tick's clock, facts.observed_at; None reads no "
            "due rows and a due row already in it is not requested again (OMN-26549)."
        ),
    )


class ModelTickLivenessRead(BaseModel):
    """One pull request head's automation-liveness read, made beside the comparison (OMN-26024).

    Observation only: the read changes no decision, no comparison row and no write. A read that could not be
    made carries read=unreadable and why in detail; a made read's event that could not be published carries
    publish_error and still keeps the fact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    head_sha: str
    read: EnumLivenessRead
    undeclared_sources: tuple[str, ...] = ()
    detail: str = ""
    topic: str = ""
    publish_error: str = ""


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
    controller_tick: int | None = Field(
        default=None,
        description="The controller's finished tick compared; None when the record carried no carry.",
    )
    compared_prs: tuple[str, ...] = Field(
        default=(),
        description="The pull requests of the compared scope, sorted (OMN-23665).",
    )
    agreed_prs: tuple[str, ...] = Field(
        default=(),
        description="The compared pull requests no divergence names (OMN-23665).",
    )
    failure_reason: EnumTickFailureReason | None = None
    failure_detail: str = ""
    rerun_answers: tuple[ModelLandingRerunAnswer, ...] = Field(
        default=(),
        description="The answer to each rerun request the tick carried, in request order (OMN-23830).",
    )
    liveness_reads: tuple[ModelTickLivenessRead, ...] = Field(
        default=(),
        description="The liveness read of each compared pull request head, when the overlay turns reads on (OMN-26024); never part of the comparison.",
    )
    running_read: ModelTickRunningRead | None = Field(
        default=None,
        description="The running-read due step: its append requests, the open due rows and the tickets held out of Done; None when the tick carried no merge and no ledger text (OMN-26549).",
    )


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
    controller_tick: int | None = None
    compared_prs: int = 0
    agreed_prs: int = 0


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
    "EnumTickSkipReason",
    "EnumTickStatus",
    "ModelTickComparedEvent",
    "ModelTickFailedEvent",
    "ModelTickLivenessRead",
    "ModelTickRequest",
    "ModelTickResult",
    "ModelTickSkippedRecord",
    "ModelTickSnapshot",
]
