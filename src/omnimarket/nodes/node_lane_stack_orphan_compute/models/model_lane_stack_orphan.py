# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inventory, input and verdict for the lane stack orphan node."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneStackContainer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    state: str
    lane: str = ""
    project: str = ""
    mem_bytes: int = Field(default=0, ge=0)


class ModelLaneStackOrphanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    containers: tuple[ModelLaneStackContainer, ...]
    live_lanes: tuple[str, ...] = ()
    finished_lanes: tuple[str, ...] = ()


class ModelLaneStackFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: Literal[
        "stop",
        "keep_live_lane",
        "keep_protected",
        "keep_unknown_lane",
        "already_stopped",
        "unowned",
    ]
    reason: str
    stop_command: str | None = None
    restore_command: str | None = None


class ModelLaneStackOrphanVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelLaneStackFinding, ...]
    stop_names: tuple[str, ...]
    unowned_names: tuple[str, ...]
    mem_bytes_freed: int
