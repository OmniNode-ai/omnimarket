# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_reading_effect: how a probe reads the host's files."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

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


__all__ = ["PathKind", "ProtocolHostFiles"]
