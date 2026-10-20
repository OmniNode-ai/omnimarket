# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the controller's host hands the receipt installer."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelLandingTlcSyncRequest(BaseModel):
    """The gate to verify with, the landing state directory and the repository of the releases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_script: Path
    state_dir: Path
    repo: str
    keep_newest: int = 3
