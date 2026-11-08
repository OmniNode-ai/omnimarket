# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of the entry: one command, run on one host."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_fill_orchestrator.models.enum_floor_fill_command import (
    EnumFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_answer import (
    ModelFloorFillAnswer,
)


class ModelFloorFillCommandRequest(BaseModel):
    """``host_id`` is the host this entry runs on, as the caller read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: EnumFloorFillCommand
    key: str | None = None
    host_id: str = Field(min_length=1)


class ModelFloorFillEntryResult(BaseModel):
    """What the entry prints (``stdout``) and exits with (``exit_code``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int = Field(ge=0)
    stdout: str
    answer: ModelFloorFillAnswer | None = None
    forwarded: bool = False
    timed_out: bool = False


__all__: list[str] = ["ModelFloorFillCommandRequest", "ModelFloorFillEntryResult"]
