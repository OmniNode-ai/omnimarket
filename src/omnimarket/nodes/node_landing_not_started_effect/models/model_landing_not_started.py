# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record refused dispatches and refund their dispatch budgets without recording worker failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelLandingNotStartedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state_dir: Path
    outcomes: tuple[dict[str, Any], ...]
    refused: tuple[str, ...]
    now: str
    tick: int


class ModelLandingNotStartedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[dict[str, Any], ...]
