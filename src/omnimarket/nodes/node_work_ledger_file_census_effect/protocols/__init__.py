# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_work_ledger_file_census_effect."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class ProtocolRepositorySource(Protocol):
    """The tracked text files of registered repositories. A failed read raises OSError."""

    def present(self, repository: str) -> bool: ...

    def candidates(self, repository: str, tokens: Sequence[str]) -> tuple[str, ...]:
        """Paths of tracked files that may name any token (a superset is allowed)."""
        ...

    def read(self, repository: str, path: str) -> str | None:
        """The file's text, or None when it is binary or over the size bound."""
        ...


class ProtocolRatchetStore(Protocol):
    """The shrink-only ratchet file. A failed read raises OSError or ValueError."""

    def read(self) -> dict[str, int]: ...


__all__ = ["ProtocolRatchetStore", "ProtocolRepositorySource"]
