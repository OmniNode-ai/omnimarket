# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's request, its read plan and its result (OMN-24171). Pure models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_eval_lane import EnumEvalLane
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
    ModelEvalItemRow,
    ModelEvalRateRow,
    ModelEvalRunRecord,
    ModelEvalTraceRow,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _contract_topic(event_name: str) -> str:
    """The one topic the node's contract subscribes to for ``event_name``; the contract owns the topic strings."""
    topics = yaml.safe_load(_CONTRACT_PATH.read_text())["event_bus"]["subscribe_topics"]
    (topic,) = (t for t in topics if t.split(".")[-2] == event_name)
    return str(topic)


TOPIC_CODE_TRY = _contract_topic("context-roi-run-completed")
TOPIC_JUDGED = _contract_topic("delegation-acceptance-judged")
TOPIC_RUN_CLOSED = _contract_topic("code-eval-run-closed")
TOPIC_TRACE = _contract_topic("eval-item-traced")
TOPIC_TICK = "onex.intent.platform.runtime-tick.v1"  # onex-topic-allow: asserted against the contract
TOPIC_SNAPSHOT = "onex.snapshot.projection.eval-results.v1"  # onex-topic-allow: projection snapshot topic

SOURCE_TOPICS: tuple[str, ...] = (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_TRACE,
)


class ModelEvalRefusedPart(BaseModel):
    """One refusal: what was refused and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumEvalRefusal
    subject: str
    detail: str = ""


class ModelEvalReadPlan(BaseModel):
    """What the writer must read before the fold can decide one delivery.

    ``refusal`` is set when the delivery cannot be read at all (a malformed uuid, a tenant that
    disagrees with the envelope): the writer reads and writes nothing and routes it to the DLQ.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    run_id: str = ""
    model_id: str = ""
    scoring_id: str = ""
    refusal: ModelEvalRefusedPart | None = None


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


class ModelEvalProjectionResult(BaseModel):
    """What the writer persists: only rows that change the tables, and the refusals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelEvalItemRow, ...] = ()
    traces: tuple[ModelEvalTraceRow, ...] = ()
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
