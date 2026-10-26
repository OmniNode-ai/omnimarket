# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify deadline kills using placement time and bound missing-result gates."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelLandingPlacementWaitRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    worker_dir: Path
    lease: Mapping[str, Any]
    engine_rc: int
    now: str


class ModelLandingPlacementWaitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    cause: str
