# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire types for the Stop gate and the answer service's cause claim."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelUnansweredGuardRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str
    transcript_path: str
    state_dir: str
    environ: dict[str, str]
    silence_tick: bool = False


class ModelAnswerHandoffRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str
    message_id: str
    cause: str
    words: str | None


class ModelAnswerHandoffResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    cause: str
    owner: str
    claim_id: str
    lane_id: str
    running: bool


class ModelUnansweredGuardResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["GateBlock", "GateHandoff", "EndTurn", "Alarm"]
    blocked_ids: tuple[str, ...] = ()
    handed_off_ids: tuple[str, ...] = ()
    reason: str = ""
    alarm: str = ""

    @property
    def allowed(self) -> bool:
        return not self.blocked_ids


class ModelUnansweredComputeBinding(BaseModel):
    """The detector dependency's contract route, selected by the hook overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    handler: str
    input_model: str
    command_topic: str
    terminal_topic: str
    group: str
    timeout_seconds: float
