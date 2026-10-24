# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One owner-summary command and its inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelOwnerSummaryRequest(BaseModel):
    """Which command to run and the paths and instants it reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: Literal["facts", "check", "triage", "mark-sent"]
    root: Path
    ledger: Path | None = None
    watcher_state: Path | None = None
    state_file: Path | None = None
    since: str = ""
    until: str = ""
    friction_data: Path | None = None
    draft: Path | None = None
    facts_file: Path | None = None
    findings: Path | None = None
    now: str = ""
    full_list_link: str = ""
