# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_alarm: the manifest, the broker's lag and the previous sample."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class ManifestUnreadableError(Exception):
    """The runtime's served manifest could not be read; the message names the cause."""


class GroupNeverCommittedError(Exception):
    """The group holds no committed offset on any partition."""


class GroupUnreadableError(Exception):
    """The group's lag could not be read (refused, broker down); the message names the type."""


class RunRecordUnwritableError(Exception):
    """The run record could not be appended; the message names the type of the failure, never its text."""


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


class ProtocolRunRecordStore(Protocol):
    def append(self, record: Mapping[str, Any]) -> None:
        """Add one finished run to the record the floor reads, or raise RunRecordUnwritableError."""
        ...


__all__ = [
    "GroupNeverCommittedError",
    "GroupUnreadableError",
    "ManifestUnreadableError",
    "ProtocolGroupLagReader",
    "ProtocolManifestSource",
    "ProtocolRunRecordStore",
    "ProtocolSampleStore",
    "RunRecordUnwritableError",
]
