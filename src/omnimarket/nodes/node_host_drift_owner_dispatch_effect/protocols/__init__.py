# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_drift_owner_dispatch_effect: the open items, the live lanes, the dispatch record, the briefs and the lane runner."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftState,
    ModelLiveLanes,
    ModelPlaceOutcome,
)


class DriftStateUnreadableError(Exception):
    """The checker's state file is absent or malformed; the message names the path."""


class LiveLanesUnreadableError(Exception):
    """The ledger read that shows the live lanes failed; the message names why."""


class ProtocolDriftOpenItems(Protocol):
    def read(self) -> ModelDriftState:
        """The host and the open items of its checker; raises DriftStateUnreadableError."""
        ...


class ProtocolLiveLanes(Protocol):
    def read(self, cause_keys: tuple[str, ...]) -> ModelLiveLanes:
        """The live lanes that name any of the cause keys; raises LiveLanesUnreadableError."""
        ...


class ProtocolDispatchRecord(Protocol):
    def load(self) -> dict[str, str]:
        """Cause key -> UTC stamp of its last started dispatch; empty when none."""
        ...

    def save(self, record: dict[str, str]) -> None: ...


class ProtocolBriefWriter(Protocol):
    def write(self, name: str, text: str) -> str:
        """Write one brief; returns its path."""
        ...


class ProtocolLaneRunner(Protocol):
    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        """Start one lane detached; a refusal is an outcome with started false, never a silent return."""
        ...


__all__ = [
    "DriftStateUnreadableError",
    "LiveLanesUnreadableError",
    "ProtocolBriefWriter",
    "ProtocolDispatchRecord",
    "ProtocolDriftOpenItems",
    "ProtocolLaneRunner",
    "ProtocolLiveLanes",
]
