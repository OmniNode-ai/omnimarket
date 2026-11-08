# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two dependencies the refresh handler is given (OMN-23033)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_repo_state_watcher_refresh_effect.models.model_watcher_refresh import (
    ModelWatcherRefreshStep,
)


class ProtocolWatcherStateClock(Protocol):
    """The read time of this host's PR watcher state."""

    def read_at(self) -> datetime | None: ...


class ProtocolRefreshStepRunner(Protocol):
    """Run one declared step to completion."""

    def run(self, name: str, argv: Sequence[str]) -> ModelWatcherRefreshStep: ...


__all__ = ["ProtocolRefreshStepRunner", "ProtocolWatcherStateClock"]
