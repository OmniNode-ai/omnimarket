# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's request, result, watcher read and failure event (OMN-23409).

One tick's facts and the decision made from them come in; the step fills each pull request's
GraphQL node id and its head's failed workflow run ids from the declared watcher source, and
records the filled tick as one ModelTickSnapshot on the snapshot topic, one record per tick, so
a later tick never replaces an earlier one.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import ModelLandingDecision, ModelLandingFacts
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    SHA_PATTERN,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)


class EnumSnapshotStatus(StrEnum):
    RECORDED = "recorded"
    FAILED = "failed"


class EnumSnapshotFailureReason(StrEnum):
    """Why a tick was not recorded. Nothing is published on the snapshot topic for it."""

    WATCHER_UNREADABLE = "watcher_unreadable"
    RECORD_REFUSED = "record_refused"


class ModelWatcherRedRun(BaseModel):
    """One red check of the watcher's CI record: its conclusion and the workflow run it ran in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    run_id: int = Field(ge=1, description="The workflow run id (actions/runs/<id>).")


class ModelWatcherPrRead(BaseModel):
    """What the watcher holds for one pull request that the snapshot fills from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str | None = Field(
        default=None, min_length=1, description="The PR's GraphQL node id."
    )
    ci_head: str | None = Field(
        default=None,
        pattern=SHA_PATTERN,
        description="The head the watcher's CI record was read at.",
    )
    red_runs: tuple[ModelWatcherRedRun, ...] = ()
    read_at: str = Field(default="", description="When the watcher read the CI record.")


class ModelLandingSnapshotRequest(BaseModel):
    """One tick to record: the facts the tick read and the decision it made from them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    facts: ModelLandingFacts
    decision: ModelLandingDecision
    fair_share: bool = Field(
        default=True,
        description="Whether the tick shared free slots across repositories.",
    )


class ModelLandingSnapshotResult(BaseModel):
    """The recorded snapshot and what was filled, or the typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    status: EnumSnapshotStatus
    tick: int
    snapshot: ModelTickSnapshot | None = Field(
        default=None, description="The record published on the snapshot topic."
    )
    node_ids_filled: int = 0
    prs_with_failed_runs: int = 0
    unread_prs: tuple[str, ...] = Field(
        default=(), description="PRs the watcher holds no record of: left as they came."
    )
    other_head_prs: tuple[str, ...] = Field(
        default=(),
        description="PRs whose watcher CI record is of another head: no run ids taken.",
    )
    failure_reason: EnumSnapshotFailureReason | None = None
    failure_detail: str = ""


class ModelLandingSnapshotFailedEvent(BaseModel):
    """Emitted when a tick could not be recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int
    reason: EnumSnapshotFailureReason
    detail: str


__all__: list[str] = [
    "EnumSnapshotFailureReason",
    "EnumSnapshotStatus",
    "ModelLandingSnapshotFailedEvent",
    "ModelLandingSnapshotRequest",
    "ModelLandingSnapshotResult",
    "ModelWatcherPrRead",
    "ModelWatcherRedRun",
]
