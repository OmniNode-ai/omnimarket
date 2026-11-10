# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_delegation_events_defect_scan_effect: the delegation_events reader."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanFact


class ModelDefectEventsRead(BaseModel):
    """The rows of the day and the time they were read at (the source's own clock), or why they were not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime | None = None
    events: tuple[ModelDefectScanFact, ...] = ()
    error: str | None = None


class ProtocolDefectEventSource(Protocol):
    def read(self, window_seconds: int) -> ModelDefectEventsRead:
        """The rows of the last ``window_seconds``; never raises, a failure is ``error``."""
        raise NotImplementedError


__all__ = ["ModelDefectEventsRead", "ProtocolDefectEventSource"]
