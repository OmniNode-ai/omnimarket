# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one orchestrator round run."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelSessionRoundRequest(BaseModel):
    """One start of a round: what to run, where its lock and heartbeat live, and its limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    state_dir: Path
    prompt: str = Field(min_length=1)
    claude: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    claude_arguments: tuple[str, ...] = ()


class ModelSessionRoundResult(BaseModel):
    """What one start did: ran the round (finished or timed out), or refused because one is alive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round_name: str
    outcome: Literal["finished", "timed-out", "busy", "not-started"]
    exit_code: int
    holder_pid: str = ""
    heartbeat_file: Path
    detail: str = ""
