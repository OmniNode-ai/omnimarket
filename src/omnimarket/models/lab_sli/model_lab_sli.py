# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of the lab service-level indicators (collector, evaluator, alert, board).

The indicator table itself is contract data in ``node_lab_sli_compute``; these
models only carry its rows, the collector's readings and the per-interval
verdicts between the nodes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_lab_sli import (
    EnumLabSliActionKind,
    EnumLabSliComparator,
    EnumLabSliSeverity,
    EnumLabSliStatus,
)


class ModelLabSliDefinition(BaseModel):
    """One row of the indicator table: target, window, severity and action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sli_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    comparator: EnumLabSliComparator
    target: float
    window_minutes: int = Field(ge=1)
    severity: EnumLabSliSeverity
    action: str = Field(min_length=1)
    source: str = Field(min_length=1)


class ModelLabSliSubjectReading(BaseModel):
    """One measured value for one subject (a repo, a host, a job)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    value: float


class ModelLabSliReading(BaseModel):
    """What the collector read for one indicator in one interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sli_id: str = Field(min_length=1)
    # Empty means the source was read and the precondition did not hold.
    subjects: tuple[ModelLabSliSubjectReading, ...] = ()
    applicable: bool = True
    detail: str = ""


class ModelLabSliReadingBatch(BaseModel):
    """``onex.cmd.omnimarket.lab-sli-evaluate-requested.v1``: one collection interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start: datetime
    collected_at: datetime
    collector_host: str = Field(min_length=1)
    readings: tuple[ModelLabSliReading, ...] = ()
    # Sources the collector could not read, with the reason.
    source_errors: tuple[str, ...] = ()


class ModelLabSliEvaluated(BaseModel):
    """``onex.evt.omnimarket.lab-sli-evaluated.v1``: one indicator, one interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sli_id: str
    title: str
    unit: str
    severity: EnumLabSliSeverity
    status: EnumLabSliStatus
    target: float
    comparator: EnumLabSliComparator
    window_minutes: int
    action: str
    interval_start: datetime
    # The worst subject's value; None when UNKNOWN or NOT_APPLICABLE.
    value: float | None = None
    worst_subject: str = ""
    breaching_subjects: tuple[str, ...] = ()
    subjects: tuple[ModelLabSliSubjectReading, ...] = ()
    detail: str = ""


class ModelLabSliEvaluationBatch(BaseModel):
    """Every indicator of the table for one interval, plus the readings refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start: datetime
    collector_host: str
    evaluations: tuple[ModelLabSliEvaluated, ...]
    # Readings naming an indicator absent from the table, or repeated.
    rejected: tuple[str, ...] = ()
    source_errors: tuple[str, ...] = ()


class ModelLabSliAlertAction(BaseModel):
    """One thing the alert node asks for: a push, a fix lane or a batched fix lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLabSliActionKind
    severity: EnumLabSliSeverity
    sli_ids: tuple[str, ...]
    dedup_key: str
    lane: str = ""
    text: str


class ModelLabSliAlertState(BaseModel):
    """Per-indicator memory between intervals: when it last fired, at what severity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_fired: dict[str, datetime] = Field(default_factory=dict)
    firing: dict[str, EnumLabSliSeverity] = Field(default_factory=dict)


class ModelLabSliAlertRequest(BaseModel):
    """An evaluation batch and the alert state the previous interval left."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation: ModelLabSliEvaluationBatch
    state: ModelLabSliAlertState = Field(default_factory=ModelLabSliAlertState)


class ModelLabSliFixLaneRequested(BaseModel):
    """One fix lane the alert asks for, carried on the ``lab-sli-alerted`` event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    severity: EnumLabSliSeverity
    sli_ids: tuple[str, ...]
    brief: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    correlation_id: UUID


class ModelLabSliDelivery(BaseModel):
    """Whether one action reached its command topic, and why not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedup_key: str
    topic: str
    published: bool
    error: str = ""


class ModelLabSliAlertResult(BaseModel):
    """``onex.evt.omnimarket.lab-sli-alerted.v1``: actions taken, suppressed and cleared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start: datetime
    actions: tuple[ModelLabSliAlertAction, ...] = ()
    # Operator pushes published on the slack-publish command, with any error.
    deliveries: tuple[ModelLabSliDelivery, ...] = ()
    # Fix lanes requested, for the lab-fill launcher to dispatch.
    fix_lanes: tuple[ModelLabSliFixLaneRequested, ...] = ()
    suppressed: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ()
    state: ModelLabSliAlertState


class ModelLabSliHistoryPoint(BaseModel):
    """One projected point: an indicator's verdict at one interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sli_id: str
    interval_start: datetime
    status: EnumLabSliStatus
    severity: EnumLabSliSeverity
    value: float | None = None
    worst_subject: str = ""


class ModelLabSliProjection(BaseModel):
    """The kept history the board renders from, newest last per indicator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: tuple[ModelLabSliHistoryPoint, ...] = ()
    latest: tuple[ModelLabSliEvaluated, ...] = ()


class ModelLabSliBoardRequest(BaseModel):
    """Fold one evaluation batch into the projection and render the board."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation: ModelLabSliEvaluationBatch
    projection: ModelLabSliProjection = Field(default_factory=ModelLabSliProjection)


class ModelLabSliBoard(BaseModel):
    """The updated projection and the one-page board rendered from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection: ModelLabSliProjection
    html: str
    markdown: str
