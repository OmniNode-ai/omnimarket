# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_alarm: the manifest, the broker's lag and the previous sample."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol


class ManifestUnreadableError(Exception):
    """The runtime's served manifest could not be read; the message names the cause."""


class ManifestUnreachableError(ManifestUnreadableError):
    """The manifest endpoint did not answer (refused, reset, timed out); no body was read.

    Distinct from a body that arrived and was refused: an unreachable endpoint says nothing about lag,
    and during a dev redeploy it is expected for the minutes the runtime takes to come up.
    """


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


class ProtocolManifestCache(Protocol):
    def load(self) -> tuple[Mapping[str, Any], datetime] | None:
        """The last good manifest (the contracts the declared nodes need) and when it was fetched, or None."""
        ...

    def save(self, manifest: Mapping[str, Any], fetched_at: datetime) -> None: ...


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
    "ManifestUnreachableError",
    "ManifestUnreadableError",
    "ProtocolGroupLagReader",
    "ProtocolManifestCache",
    "ProtocolManifestSource",
    "ProtocolRunRecordStore",
    "ProtocolSampleStore",
    "RunRecordUnwritableError",
]
