# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory two-strike store the fix effect runs on when none is injected (OMN-21695).

Real merge-sweep wiring injects ``JsonFileTwoStrikeStore`` via the orchestrator's
``_ensure_sub_handlers``; the handler never constructs a store itself and takes the
one this module binds for standalone and test use.
"""

from __future__ import annotations


class InMemoryTwoStrikeStore:
    """Ephemeral in-memory two-strike counter for standalone/test use.

    Does NOT persist across process restarts. Real merge-sweep wiring injects
    ``JsonFileTwoStrikeStore`` via the orchestrator's ``_ensure_sub_handlers``
    so the two-strike threshold survives across ticks (safety bar #7).
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def get_strikes(self, key: str) -> int:
        return self._counts.get(key, 0)

    def record_failure(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]


def bind_two_strike_store() -> InMemoryTwoStrikeStore:
    """Bind a fresh in-memory two-strike store."""
    return InMemoryTwoStrikeStore()
