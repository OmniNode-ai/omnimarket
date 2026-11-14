# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_drift_owner_dispatch_effect: the open items, the live lanes, the dispatch record, the briefs and the lane runner."""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelBriefPrepared,
    ModelDriftState,
    ModelLiveLanes,
    ModelPlaceOutcome,
)


class DriftStateUnreadableError(Exception):
    """The checker's state file is absent or malformed; the message names the path."""


class LiveLanesUnreadableError(Exception):
    """The ledger read that shows the live lanes failed; the message names why."""


class BriefEnvironmentAbsentError(Exception):
    """A setting the brief check reads is in neither the unit's environment nor the store; the message names the setting and the folder, never a value."""


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


class ProtocolBriefPreparer(Protocol):
    def prepare(self, *, lane: str, name: str, text: str) -> ModelBriefPrepared:
        """Complete the task text into a lane brief (the standing rules block, the ticket text and the 3a
        constraints), write it as ``name`` and run the lane-brief check on the written bytes. A check that
        refused or could not run is a result with ``passed`` false and its reasons, never a silent pass."""
        ...


class ProtocolLaneRunner(Protocol):
    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        """Start one lane detached; a refusal is an outcome with started false, never a silent return."""
        ...


class ProtocolStoreReader(Protocol):
    async def read(self, folder: str, name: str) -> SecretStr | None:
        """The value of the store entry named ``name`` in ``folder``; None when the entry is absent or blank."""
        ...


__all__ = [
    "BriefEnvironmentAbsentError",
    "DriftStateUnreadableError",
    "LiveLanesUnreadableError",
    "ProtocolBriefPreparer",
    "ProtocolDispatchRecord",
    "ProtocolDriftOpenItems",
    "ProtocolLaneRunner",
    "ProtocolLiveLanes",
    "ProtocolStoreReader",
]
