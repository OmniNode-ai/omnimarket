# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port the scheduled feeder feeds its sources through, and the in-memory default profile."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.work_state_feeder import ModelFeederOutcome

NOT_FED = "in-memory profile: nothing was fed"


@runtime_checkable
class ProtocolWorkStateFeeder(Protocol):
    """One feeding for one cycle: reads the sources and writes where the floor reads."""

    def feed(self, now: datetime) -> ModelFeederOutcome: ...


class NotFedWorkStateFeeder:
    """The in-memory default profile: no source was read, so nothing was fed."""

    def feed(self, now: datetime) -> ModelFeederOutcome:
        return ModelFeederOutcome(status="not_fed", item_count=0, cause=NOT_FED)


__all__: list[str] = ["NotFedWorkStateFeeder", "ProtocolWorkStateFeeder"]
