# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the tick looks for the hook-emit drainer and how it reinstalls it (OMN-19998)."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DRAINER_LABEL = "ai.omninode.hook-emit-drainer"


class ModelDrainerWatchOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The name the alarm row gives this host.
    host: str
    # The drainer's state root: its status file and its journal live here.
    state_dir: Path
    # The installer that renders the job, builds its interpreter and loads it.
    installer: Path
    home: Path
    registry_root: Path
    # auto picks launchd where launchctl exists and systemd --user otherwise.
    supervisor: Literal["auto", "launchd", "systemd"] = "auto"
    label: str = DRAINER_LABEL
    uid: int | None = None
    probe_timeout_seconds: int = Field(default=30, gt=0)
    repair_timeout_seconds: int = Field(default=600, gt=0)
    # How long after a reinstall the tick waits for the job to report running.
    settle_seconds: int = Field(default=10, ge=0)
    stale_minutes: int = Field(default=15, gt=0)
