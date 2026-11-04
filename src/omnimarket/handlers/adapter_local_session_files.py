# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local disk behind ProtocolSessionFiles (OMN-22468); the only place the session compute nodes' file access really happens."""

from __future__ import annotations

from pathlib import Path

__all__ = ["LocalSessionFiles"]


class LocalSessionFiles:
    """The local disk behind ProtocolSessionFiles (OMN-22468); the only place the session compute nodes' file access really happens."""

    def read_text(
        self, path: Path, *, encoding: str = "utf-8", errors: str = "strict"
    ) -> str:
        """Read text from a file path."""
        return path.read_text(encoding=encoding, errors=errors)

    def read_bytes(self, path: Path) -> bytes:
        """Read bytes from a file path."""
        return path.read_bytes()

    def write_text(self, path: Path, data: str, *, encoding: str = "utf-8") -> None:
        """Write text to a file path."""
        path.write_text(data, encoding=encoding)
