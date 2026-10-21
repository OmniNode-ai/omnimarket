# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What CI hands the receipt builder after the model check ran."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelLandingTlcPublishRequest(BaseModel):
    """The controller's gate and pin, and the raw output of the model check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_script: Path
    pin_file: Path
    results_dir: Path
    model: Path
    host: str
    out_dir: Path
