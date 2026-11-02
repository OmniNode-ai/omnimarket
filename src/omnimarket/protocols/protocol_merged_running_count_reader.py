# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one read the factory floor makes of the merged-and-running count, so the floor imports no other node."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
)


@runtime_checkable
class ProtocolMergedRunningCountReader(Protocol):
    """Read the count as of ``now``; raise when a source the count needs cannot be read."""

    def read(
        self, now: datetime, *, hosts_table: Path | None = None
    ) -> ModelMergedRunningCountResult:
        """Use the caller's resolved host table when supplied, else the reader's configuration."""
        ...


__all__: list[str] = ["ProtocolMergedRunningCountReader"]
