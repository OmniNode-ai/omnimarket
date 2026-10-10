# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether an order edge fired after a head's red was graded (pure, definition-B)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_fired_edge_facts import (
    ModelFiredEdgeFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_fired_edge_verdict import (
    ModelFiredEdgeVerdict,
)


def fired_edge_graded_stale(
    parent_states: Mapping[str, Mapping[str, Any]], last_red_completed_at: str
) -> bool:
    """Whether an order edge fired after this head's red was graded (OMN-19998).

    True only when there is at least one predecessor, every one is MERGED (closed is not merged) with a
    ``merged_at``, and the newest ``merged_at`` is later than the head's last red completion. A missing
    stamp on either side is not proof of a stale grade, so it is False (the red stays ``product``).
    ISO-8601 UTC ``Z`` stamps of one shape compare as strings.
    """
    if not parent_states or not last_red_completed_at:
        return False
    stamps = []
    for facts in parent_states.values():
        if str(facts.get("state") or "") != "MERGED" or not facts.get("merged_at"):
            return False
        stamps.append(str(facts["merged_at"]))
    return max(stamps) > str(last_red_completed_at)


class HandlerFiredEdgeGradedStale:
    """Whether the head's red was graded before its predecessors merged (pure, definition-B)."""

    def handle(self, request: ModelFiredEdgeFacts) -> ModelFiredEdgeVerdict:
        return ModelFiredEdgeVerdict(
            stale=fired_edge_graded_stale(
                {
                    p: f.model_dump(exclude_unset=True)
                    for p, f in request.parent_states.items()
                },
                request.last_red_completed_at or "",
            )
        )


__all__: list[str] = ["HandlerFiredEdgeGradedStale", "fired_edge_graded_stale"]
