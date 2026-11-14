# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_hook_lane_trace_compute.models.model_lane_trace_row import (
    ModelLaneTraceRow,
)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TRACE_LIMIT = 500


class ModelLaneTraceRequest(BaseModel):
    """One lane and one window; with rows, the trace of those rows, without them only the query that reads them.

    The lane name must be a plain token and both bounds UTC stamps, so nothing else can reach the query text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    since: str
    until: str
    limit: int = TRACE_LIMIT
    rows: tuple[ModelLaneTraceRow, ...] | None = None

    @field_validator("lane")
    @classmethod
    def _plain_lane(cls, value: str) -> str:
        if not TOKEN_RE.fullmatch(value):
            raise ValueError(
                f"lane trace refuses lane name {value!r}: it must be a plain token ([A-Za-z0-9_.-]+)"
            )
        return value

    @field_validator("since")
    @classmethod
    def _since_stamp(cls, value: str) -> str:
        if not STAMP_RE.fullmatch(value):
            raise ValueError(
                f"lane trace refuses start bound {value!r}: it must be a UTC stamp YYYY-MM-DDTHH:MM:SSZ"
            )
        return value

    @field_validator("until")
    @classmethod
    def _until_stamp(cls, value: str) -> str:
        if not STAMP_RE.fullmatch(value):
            raise ValueError(
                f"lane trace refuses end bound {value!r}: it must be a UTC stamp YYYY-MM-DDTHH:MM:SSZ"
            )
        return value

    @field_validator("limit")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"lane trace refuses limit {value!r}: it must be a positive integer"
            )
        return value
