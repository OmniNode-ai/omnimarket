# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where one thermal observation is recorded."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelHostThermalObserveRequest:
    """One observation of the host the handler runs on, appended to the host's own series."""

    host: str
    state_dir: Path
    sample_seconds: float = 1.0
    dry_run: bool = False
