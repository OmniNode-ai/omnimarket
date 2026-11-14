# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compact snapshot of the eval-results projection and its limits (OMN-24171).

The snapshot carries only the tracking runs of the models the display overlay names, a closed
run as its rate rows and a void run as a date and cause only, and each model's last live-work
weeks. No unbounded list rides on it: a run carries ``task_sample_digest``, never its task ids,
and a live week carries a count and a digest of its reviewer versions, never the list.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)


class ModelEvalResultsOverlay(BaseModel):
    """The node's overlay. No key has a default: the first values are the plan's, set by a deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_names: dict[str, str] = Field(
        description="evals.board.display_names: model id to plain label; only these models reach the snapshot."
    )
    max_models: int = Field(ge=1, description="evals.board.max_models")
    snapshot_runs: int = Field(ge=1, description="evals.projection.snapshot_runs")
    live_weeks: int = Field(ge=1, description="evals.projection.live_weeks")
    max_field_chars: int = Field(ge=1, description="evals.projection.max_field_chars")
    snapshot_max_bytes: int = Field(
        ge=1, description="evals.projection.snapshot_max_bytes"
    )
    publish_interval_s: int = Field(
        ge=1, description="evals.projection.publish_interval_s"
    )
    lane: str = Field(
        min_length=1, description="The lane stamped on every item: dev or stability."
    )
    host: str = ""
    install_revision: str = ""


class ModelEvalSnapshotRate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    size_band: str
    n: int = Field(ge=0)
    passed: int = Field(ge=0)
    unscorable_model: int = Field(ge=0)
    unscorable_infrastructure: int = Field(ge=0)
    wilson_low: float
    wilson_high: float


class ModelEvalSnapshotRun(BaseModel):
    """One tracking run of one model: its rates when it read closed, only a date and cause when void."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    state: EnumEvalRunState
    series: str
    engine: str = ""
    task_set_digest: str = ""
    task_sample_digest: str = ""
    model_revision: str = ""
    generation_settings_hash: str = ""
    prompt_template_version: str = ""
    scorer_version: str = ""
    context_arm: str = ""
    replicate: int = 0
    first_scored_at: datetime | None = None
    last_scored_at: datetime
    void_reason: str = ""
    band_usable: dict[str, int] = Field(default_factory=dict)
    band_target: dict[str, int] = Field(default_factory=dict)
    rates: tuple[ModelEvalSnapshotRate, ...] = ()


class ModelEvalSnapshotLiveWeek(BaseModel):
    """One week of live work for one model, checked by a reviewer model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    week: str
    n: int = Field(ge=0)
    passed: int = Field(ge=0)
    wilson_low: float
    wilson_high: float
    first_scored_at: datetime
    last_scored_at: datetime
    reviewer_version_count: int = Field(ge=0)
    reviewer_versions_digest: str


class ModelEvalSnapshotModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    runs: tuple[ModelEvalSnapshotRun, ...] = ()
    live_weeks: tuple[ModelEvalSnapshotLiveWeek, ...] = ()


class ModelEvalSnapshot(BaseModel):
    """``onex.snapshot.projection.eval-results.v1``: stamped ``published_at``; each run keeps its own run times."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published_at: datetime
    tenant_id: str
    models: tuple[ModelEvalSnapshotModel, ...] = ()


class EnumEvalSnapshotRefusal(StrEnum):
    OVER_BOUND = "over_bound"
    FIELD_TOO_LONG = "field_too_long"


class EvalSnapshotRefusedError(ValueError):
    """The snapshot was refused, typed: over its byte bound (never cut), or a text field over its length."""

    def __init__(
        self,
        kind: EnumEvalSnapshotRefusal,
        detail: str,
        *,
        size: int = 0,
        limit: int = 0,
    ) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.size = size
        self.limit = limit


__all__ = [
    "EnumEvalSnapshotRefusal",
    "EvalSnapshotRefusedError",
    "ModelEvalResultsOverlay",
    "ModelEvalSnapshot",
    "ModelEvalSnapshotLiveWeek",
    "ModelEvalSnapshotModel",
    "ModelEvalSnapshotRate",
    "ModelEvalSnapshotRun",
]
