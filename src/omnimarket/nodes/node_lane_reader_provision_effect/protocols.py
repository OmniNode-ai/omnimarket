# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host boundary of the lane reader provisioning effect (dependency injection)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ProtocolLaneReaderHost(Protocol):
    """The filesystem and process operations the effect performs on its host.

    ``read_text`` and ``write_text_atomic`` carry whole files; the effect never logs what they hold.
    """

    def exists(self, path: str) -> bool:
        """True when the path names anything, a link included."""
        ...

    def read_text(self, path: str) -> str | None:
        """The file's text, or ``None`` when it is absent."""
        ...

    def write_text_atomic(self, path: str, text: str) -> None: ...

    def link_target(self, path: str) -> str | None:
        """Where a symlink points, or ``None`` when ``path`` is not a symlink."""
        ...

    def replace_link(self, path: str, target: str) -> None: ...

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        """Run argv; return its exit status and the tail of its output."""
        ...
