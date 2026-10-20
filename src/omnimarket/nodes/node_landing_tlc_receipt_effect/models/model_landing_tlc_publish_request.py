# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What CI hands the receipt builder after the model check ran."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLandingTlcPublishRequest:
    """The controller's gate and pin, and the raw output of the model check."""

    gate_script: Path
    pin_file: Path
    results_dir: Path
    model: Path
    host: str
    out_dir: Path
