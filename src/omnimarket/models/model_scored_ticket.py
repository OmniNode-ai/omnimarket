# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared re-export of the RSD scoring model for cross-node consumption.

Consumers (e.g. node_skill_dispatch_engine_orchestrator) import ``ModelScoredTicket``
from here instead of reaching into node_rsd_fill_compute's private models package
directly. Mirrors the established omnimarket.events.dep_health pattern and keeps the
cross-node reach-in guard (tests/test_no_cross_node_reach_in.py) satisfied without
growing its allowlist. The canonical definition still lives in node_rsd_fill_compute
(its own I/O model); full physical promotion is deferred (OMN-13834).
"""

from __future__ import annotations

from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)

__all__: list[str] = ["ModelScoredTicket"]
