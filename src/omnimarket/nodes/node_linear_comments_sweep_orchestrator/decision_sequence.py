# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The count of decisions the sweep has asked of its decision node (OMN-21536).

Each decision's correlation id carries the count, so two decisions of one kind in
one run never share a correlation. The count is state, so it lives here, in a
collaborator the bus handler is given, and not on the handler: a public handler
method never assigns to the handler.
"""

from __future__ import annotations

__all__: list[str] = ["DecisionSequence"]


class DecisionSequence:
    """A running count of the decisions asked, starting at one."""

    def __init__(self) -> None:
        self._asked = 0

    def next(self) -> int:
        """Count one more decision and return the count."""
        self._asked += 1
        return self._asked
