# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure chain comparison handler — ported from onex-self-extending-agent diff_chains()."""

from __future__ import annotations

from omnimarket.nodes.node_chain_diff.models.model_chain_diff import ModelChainDiff
from omnimarket.nodes.node_chain_diff.models.model_chain_diff_request import (
    ModelChainDiffRequest,
)
from omnimarket.nodes.node_chain_diff.models.model_golden_chain_entry import (
    ModelGoldenChainEntry,
)


class HandlerChainDiff:
    """Compare two chains and return a detailed diff. No I/O, no side effects."""

    def handle(self, request: ModelChainDiffRequest) -> ModelChainDiff:
        expected: tuple[ModelGoldenChainEntry, ...] = request.expected
        observed: tuple[ModelGoldenChainEntry, ...] = request.observed

        expected_by_event = {e.event_type: e for e in expected}
        observed_by_event = {e.event_type: e for e in observed}

        missing_events = tuple(
            e for e in expected if e.event_type not in observed_by_event
        )
        unexpected_events = tuple(
            e for e in observed if e.event_type not in expected_by_event
        )

        order_mismatches: list[str] = []
        topic_mismatches: list[str] = []

        for obs_entry in observed:
            if obs_entry.event_type in expected_by_event:
                exp_entry = expected_by_event[obs_entry.event_type]
                if obs_entry.sequence != exp_entry.sequence:
                    order_mismatches.append(
                        f"{obs_entry.event_type}: expected seq {exp_entry.sequence}, observed seq {obs_entry.sequence}"
                    )
                if obs_entry.topic != exp_entry.topic:
                    topic_mismatches.append(
                        f"{obs_entry.event_type}: expected topic {exp_entry.topic!r}, observed {obs_entry.topic!r}"
                    )

        matches = (
            len(expected) == len(observed)
            and not missing_events
            and not unexpected_events
            and not order_mismatches
            and not topic_mismatches
        )

        return ModelChainDiff(
            matches=matches,
            expected_count=len(expected),
            observed_count=len(observed),
            missing_events=missing_events,
            unexpected_events=tuple(unexpected_events),
            order_mismatches=tuple(order_mismatches),
            topic_mismatches=tuple(topic_mismatches),
        )


__all__ = ["HandlerChainDiff"]
