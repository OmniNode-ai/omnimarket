# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the controller's host hands the receipt installer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLandingTlcSyncRequest:
    """The gate to verify with, the landing state directory and the repository of the releases."""

    gate_script: Path
    state_dir: Path
    repo: str
    keep_newest: int = 3
