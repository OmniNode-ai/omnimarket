# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared error for a host pool that cannot be read (OMN-23664).

It lives here, not in the lane start node's models, because both the lane start leader and the pool snapshot
node name it; one node importing another node's models is a reach-in (tests/test_no_cross_node_reach_in.py).
"""

from __future__ import annotations


class LaneHostPoolUnreadableError(RuntimeError):
    """A host pool adapter could not give a fresh, readable snapshot (missing, stale, unparseable).

    The leader ends the start ``pool-unreadable`` before any claim; it never reads the pool as empty or as full.
    """


__all__: list[str] = ["LaneHostPoolUnreadableError"]
