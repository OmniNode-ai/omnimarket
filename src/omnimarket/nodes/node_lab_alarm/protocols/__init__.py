# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_alarm: the manifest, the broker's lag and the previous sample."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    ModelEffectsGroupReading,
    ModelEscalationBrief,
)


class ManifestUnreadableError(Exception):
    """The runtime's served manifest could not be read; the message names the cause."""


class GroupNeverCommittedError(Exception):
    """The group holds no committed offset on any partition."""


class GroupUnreadableError(Exception):
    """The group's lag could not be read (refused, broker down); the message names the type."""


class ProtocolManifestSource(Protocol):
    def read(self) -> Mapping[str, Any]:
        """The running image's auto-wiring manifest, or raise ManifestUnreadableError."""
        ...


class ProtocolGroupLagReader(Protocol):
    def total_lag(self, group: str) -> int:
        """Committed offsets against end offsets, summed; or raise one of the two errors above."""
        ...


class ProtocolSampleStore(Protocol):
    def load(self) -> dict[str, int] | None:
        """The previous sample, or None when there is none."""
        ...

    def save(self, sample: Mapping[str, int]) -> None: ...


class EffectsGroupReadError(Exception):
    """The effects consumer groups could not be listed; the message names only the cause's type."""


class LaneDispatchError(Exception):
    """The lane that owns an alarm could not be started; the message names why."""


class ProtocolEffectsGroupReader(Protocol):
    def read(self) -> tuple[ModelEffectsGroupReading, ...]:
        """Every group matching the declared prefix and suffix, with its state and live member count, or raise EffectsGroupReadError."""
        ...


class ProtocolHeldStateStore(Protocol):
    def load(self) -> dict[str, str]:
        """The effects-held episode state (``held_since``, ``escalated_at``), empty when there is none."""
        ...

    def save(self, state: Mapping[str, str]) -> None: ...


class ProtocolLaneDispatcher(Protocol):
    def dispatch(self, brief: ModelEscalationBrief) -> None:
        """Start one lane for the brief, or raise LaneDispatchError."""
        ...


__all__ = [
    "EffectsGroupReadError",
    "GroupNeverCommittedError",
    "GroupUnreadableError",
    "LaneDispatchError",
    "ManifestUnreadableError",
    "ProtocolEffectsGroupReader",
    "ProtocolGroupLagReader",
    "ProtocolHeldStateStore",
    "ProtocolLaneDispatcher",
    "ProtocolManifestSource",
    "ProtocolSampleStore",
]
