# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_reading_effect: how a probe reads the host's files, the lab host table and
the route a canary lane takes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_reading import ModelCanaryHost, ModelCanaryRun

PathKind = Literal["link", "file", "directory", "missing"]


class ProtocolHostFiles(Protocol):
    """Read-only view of a host's files. A failure of the read raises OSError."""

    def kind(self, path: Path) -> PathKind: ...

    def resolve(self, path: Path) -> Path: ...

    def list_names(self, directory: Path, pattern: str) -> tuple[str, ...]:
        """Names directly in ``directory`` that match ``pattern``, sorted; none when absent."""
        ...

    def read_text(self, path: Path) -> str: ...

    def index_entries(self, root: Path, name: str) -> tuple[tuple[str, str], ...]:
        """(mode, path) of each entry a git index under ``root`` holds at or below ``name``.

        None when ``root`` is not a work tree: no restore can rewrite the name there.
        """
        ...


class ProtocolHostInventory(Protocol):
    """The lab host table: every host, pooled or parked. An unreadable table raises OSError or
    ValueError."""

    def hosts(self) -> tuple[ModelCanaryHost, ...]: ...


class ProtocolCanaryRunner(Protocol):
    """Run one canary through the route lanes take, bounded by ``timeout_seconds``.

    A deadline that passes is ``timed_out`` in the result, never an exception; a route that
    cannot be started raises OSError."""

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCanaryRun: ...


class ProtocolAdvertisementSource(Protocol):
    """The newest capacity advertisement each host published. An unreadable source raises
    OSError or ValueError."""

    def read(self) -> tuple[ModelHostCapacityAdvertisement, ...]: ...


__all__ = [
    "PathKind",
    "ProtocolAdvertisementSource",
    "ProtocolCanaryRunner",
    "ProtocolHostFiles",
    "ProtocolHostInventory",
]
