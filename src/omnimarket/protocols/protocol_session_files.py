# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem port of node_session_compute nodes (OMN-22468)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["ProtocolSessionFiles"]


@runtime_checkable
class ProtocolSessionFiles(Protocol):
    """The file access of the four session compute nodes (OMN-22468): node_session_front_door_compute, node_session_checkpoint_compute, node_session_resume_compute and node_team_identity_compute. A compute node reads and writes only through this port, so the node-purity gate (OMN-13283) holds."""

    def read_text(
        self, path: Path, *, encoding: str = "utf-8", errors: str = "strict"
    ) -> str:
        """Read text from a file path."""
        ...

    def read_bytes(self, path: Path) -> bytes:
        """Read bytes from a file path."""
        ...

    def write_text(self, path: Path, data: str, *, encoding: str = "utf-8") -> None:
        """Write text to a file path."""
        ...
