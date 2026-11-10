# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared by the seam nodes: what came back for a demanded command, and the demand itself."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSeamCrossing(StrEnum):
    """What came back for one demanded command."""

    RESULT = "result"
    RESULT_ERROR = "result-error"
    NO_RESULT_BY_DEADLINE = "no-result-by-deadline"
    BUS_UNREADABLE = "bus-unreadable"
    #: An on-demand seam whose node could not be attached, so its command was never sent.
    ATTACH_FAILED = "attach-failed"


class ModelSeamDemand(BaseModel):
    """One command to send and the result topic to read for its correlated answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam: str = Field(min_length=1)
    command_topic: str = Field(min_length=1)
    result_topic: str = Field(min_length=1)
    consumer_group_prefix: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    payload: dict[str, str | int | bool]
    error_keys: tuple[str, ...] = Field(min_length=1)
    deadline_seconds: float = Field(ge=0)
    late_listen_seconds: float = Field(default=0.0, ge=0)


__all__ = ["EnumSeamCrossing", "ModelSeamDemand"]
