# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The organization, complete UTC day and operator state paths to check."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelHostedMinutesWatchRequest(BaseModel):
    """One billing read for the requested UTC day's month."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org: str = "OmniNode-ai"
    day: date
    state_dir: Path
    ledger: Path | None = None
