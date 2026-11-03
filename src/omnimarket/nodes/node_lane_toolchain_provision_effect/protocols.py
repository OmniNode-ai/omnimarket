# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host boundary of the lane toolchain provisioning effect (dependency injection)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ProtocolLaneToolchainHost(Protocol):
    """The filesystem and process operations the effect performs on its host."""

    def exists(self, path: str) -> bool:
        """True when the path names anything, a link included."""
        ...

    def link_target(self, path: str) -> str | None:
        """Where a symlink points, or ``None`` when ``path`` is not a symlink."""
        ...

    def replace_link(self, path: str, target: str) -> None: ...

    def list_dir(self, path: str) -> tuple[str, ...] | None:
        """The entry names of a directory, or ``None`` when ``path`` is not a directory."""
        ...

    def remove_empty_dir(self, path: str) -> None: ...

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        """Run argv; return its exit status (127 when not found) and the tail of its output."""
        ...
