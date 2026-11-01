# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem port of node_replay_bundle_writer_effect (OMN-21108)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolReplayBundleFilesystem(Protocol):
    """The only file I/O the replay bundle writer performs."""

    def resolve(self, path: str) -> str:
        """Absolute path with symlinks and `..` resolved."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write the bytes, creating missing parent directories."""
        ...

    def read_bytes(self, path: str) -> bytes | None:
        """The file's bytes, or None when it does not exist."""
        ...


__all__ = ["ProtocolReplayBundleFilesystem"]
