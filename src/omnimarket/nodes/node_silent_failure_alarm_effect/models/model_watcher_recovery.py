# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host paths, bounds and recorded outcomes for watcher inventory recovery."""

from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelWatcherRecoveryOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    watcher_state: Path
    watcher_log: Path
    recovery_state: Path
    watcher_plist: Path = Field(
        default_factory=lambda: (
            Path.home() / "Library/LaunchAgents/ai.omninode.pr-watcher.plist"
        )
    )
    # Explicit argv overrides are for hosts using another declared supervisor adapter.
    # The default reads the installed watcher service's interpreter, module and bus binding.
    status_command: tuple[str, ...] = ()
    recovery_command: tuple[str, ...] = ()
    diagnostic_timeout_seconds: int = Field(default=30, gt=0, le=30)
    recovery_timeout_seconds: int = Field(default=210, gt=0, le=210)
    cooldown_seconds: int = Field(default=600, ge=480)
    log_tail_bytes: int = Field(default=65536, gt=0, le=65536)
    log_tail_lines: int = Field(default=60, gt=0, le=60)


class ModelWatcherRecoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    now: AwareDatetime
    apply: bool = True


class ModelWatcherRecoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    checked_at: AwareDatetime | None = None
    before_at: AwareDatetime | None = None
    after_at: AwareDatetime | None = None
    outcome: Literal[
        "fresh",
        "dry-run",
        "cooldown",
        "locked",
        "attempted",
        "recovered",
        "failed",
        "refused",
    ]
    inventory_status: str = ""
    log_evidence: tuple[str, ...] = ()
    recovery_exit: int | None = None
    detail: str = ""
