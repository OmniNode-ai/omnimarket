# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's request, its read plan and its result (OMN-24171). Pure models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_eval_lane import EnumEvalLane
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
    ModelEvalItemRow,
    ModelEvalRateRow,
    ModelEvalRunRecord,
    ModelEvalTraceRow,
)

TOPIC_CODE_TRY = "onex.evt.omnimarket.context-roi-run-completed.v1"  # onex-topic-allow: asserted against the contract
TOPIC_JUDGED = "onex.evt.omnimarket.delegation-acceptance-judged.v1"  # onex-topic-allow: asserted against the contract
TOPIC_RUN_CLOSED = "onex.evt.omnimarket.code-eval-run-closed.v1"  # onex-topic-allow: asserted against the contract
TOPIC_TRACE = "onex.evt.omnimarket.eval-item-traced.v1"  # onex-topic-allow: asserted against the contract
TOPIC_TICK = "onex.intent.platform.runtime-tick.v1"  # onex-topic-allow: asserted against the contract
TOPIC_SNAPSHOT = "onex.snapshot.projection.eval-results.v1"  # onex-topic-allow: projection snapshot topic

SOURCE_TOPICS: tuple[str, ...] = (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_TRACE,
)


class ModelEvalReadPlan(BaseModel):
    """What the writer must read before the fold can decide one delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    run_id: str = ""
    model_id: str = ""
    scoring_id: str = ""


class ModelEvalProjectionRequest(BaseModel):
    """One delivery, the standing rows its decision reads, and the lane stamps. No clock is read.

    ``observed_at`` is the envelope's event time: a code try row carries no time of its own, so it
    is the row's ``scored_at``; a close and a trace part are stored with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    payload: dict[str, Any]
    observed_at: datetime
    tenant_id: str
    lane: EnumEvalLane = EnumEvalLane.DEV
    host: str = ""
    install_revision: str = ""
    standing_items: tuple[ModelEvalItemRow, ...] = ()
    standing_run: ModelEvalRunRecord | None = None
    standing_parts: tuple[ModelEvalTraceRow, ...] = ()


class ModelEvalRefusedPart(BaseModel):
    """One refusal: what was refused and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumEvalRefusal
    subject: str
    detail: str = ""


class ModelEvalProjectionResult(BaseModel):
    """What the writer persists: only rows that change the tables, and the refusals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelEvalItemRow, ...] = ()
    traces: tuple[ModelEvalTraceRow, ...] = ()
    drop_scoring_ids: tuple[str, ...] = Field(
        default=(),
        description="Scorings whose stored parts must be deleted: the parts did not reassemble to their sha256.",
    )
    run_record: ModelEvalRunRecord | None = None
    rates: tuple[ModelEvalRateRow, ...] = ()
    refused: tuple[ModelEvalRefusedPart, ...] = ()


__all__ = [
    "SOURCE_TOPICS",
    "TOPIC_CODE_TRY",
    "TOPIC_JUDGED",
    "TOPIC_RUN_CLOSED",
    "TOPIC_SNAPSHOT",
    "TOPIC_TICK",
    "TOPIC_TRACE",
    "ModelEvalProjectionRequest",
    "ModelEvalProjectionResult",
    "ModelEvalReadPlan",
    "ModelEvalRefusedPart",
]
