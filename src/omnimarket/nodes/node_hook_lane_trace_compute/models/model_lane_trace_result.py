# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_hook_lane_trace_compute.models.enum_lane_call_label import (
    EnumLaneCallLabel,
)
from omnimarket.nodes.node_hook_lane_trace_compute.models.model_lane_trace_row import (
    ModelLaneTraceRow,
)


class ModelLaneTraceCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLaneTraceRow
    label: EnumLaneCallLabel
    refusing_component: str | None = None


class ModelLaneTraceCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: int
    ok: int
    failed: int
    refused: int
    timeout: int
    no_output: int


class ModelLaneTraceResult(BaseModel):
    """The READ ONLY query that reads the lane's calls and, when rows were given, the labelled calls and the printed trace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: str
    calls: tuple[ModelLaneTraceCall, ...] = ()
    counts: ModelLaneTraceCounts | None = None
    lines: tuple[str, ...] = ()
