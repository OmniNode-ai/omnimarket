# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored-backlog refresh crossing and its injected policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .model_idle_slot_fill import ModelBacklogSources


class ModelPillarBacklogPolicy(BaseModel):
    """Values supplied by the node declaration or a deployment overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[Literal["process-fix", "defect", "readback"], ...]
    fact_max_age_seconds: float = Field(gt=0)
    watcher_max_age_seconds: float = Field(gt=0)
    watcher_resync_max_age_seconds: float = Field(gt=0)
    ticket_max_age_seconds: float = Field(gt=0)
    stalled_seconds: float = Field(gt=0)
    claim_heartbeat_seconds: float = Field(gt=0)
    claim_max_age_seconds: float = Field(gt=0)
    stalled_classes: tuple[str, ...]
    defect_labels: tuple[str, ...]
    ticket_team_key: str = Field(min_length=1)
    ticket_page_size: int = Field(ge=1, le=100)
    ticket_max_pages: int = Field(gt=0)
    backlog_file: str
    dropped_file: str


@dataclass(frozen=True)
class ModelPillarBacklogRefreshRequest:
    now: str
    state_dir: Path
    watcher_state: dict[str, Any] | None
    sources: ModelBacklogSources | None = None
    backlog_path: Path | None = None
    read_only: bool = False


@dataclass(frozen=True)
class ModelPillarBacklogRefreshResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    standing_tickets: dict[str, str] = field(default_factory=dict)
    dropped: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    written: bool = False
    dropped_path: Path | None = None
