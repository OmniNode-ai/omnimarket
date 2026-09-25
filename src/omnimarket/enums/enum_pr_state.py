# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lifecycle state of a pull request, as a board-truth fact.

CLOSED means closed without merging. It is deliberately distinct from MERGED:
an abandoned PR is reaper evidence, a merged PR is completion evidence, and
collapsing the two would let abandoned work read as done.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16730: fact-source adapters that populate this
"""

from __future__ import annotations

from enum import StrEnum


class EnumPrState(StrEnum):
    """Pull-request lifecycle state."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


__all__ = [
    "EnumPrState",
]
