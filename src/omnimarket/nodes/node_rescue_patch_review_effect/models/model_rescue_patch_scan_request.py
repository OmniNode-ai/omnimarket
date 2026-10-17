# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The saved patches, registry clones and heartbeat directory."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelRescuePatchScanRequest(BaseModel):
    """The saved patches, registry clones and heartbeat directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rescue_root: Path
    registry_root: Path
    state_dir: Path
    now: str = ""
