# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected filesystem and publication boundaries for the lane context ledger dedupe."""

from typing import NamedTuple, Protocol

from pydantic import BaseModel


class FileIdentity(NamedTuple):
    """What the dedupe needs of a file: its device and inode, size, link count and mtime (epoch seconds)."""

    dev: int
    ino: int
    size: int
    nlink: int
    mtime: float


class ProtocolLaneCtxFs(Protocol):
    def context_dirs(self, runs_root: str) -> list[str]: ...

    def rolls(self, context_dir: str) -> list[str]:
        """Regular (not symlinked) archive rolls of one context directory, as full paths."""
        ...

    def identity(self, path: str) -> FileIdentity | None: ...

    def same_bytes(self, left: str, right: str) -> bool: ...

    def ensure_dir(self, path: str) -> None: ...

    def link_new(self, source: str, target: str) -> None:
        """Hard-link source to the absent path target."""
        ...

    def replace_with_link(self, shared: str, roll: str) -> None:
        """Make roll another name of shared, atomically: a reader never sees a missing roll."""
        ...


class ProtocolLaneCtxEventPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...
