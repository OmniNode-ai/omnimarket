"""Pure deterministic golden-chain comparison."""

from __future__ import annotations

from omnibase_core.models.pipeline.model_chain_diff import ModelChainDiff
from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry

from omnimarket.nodes.node_chain_diff_compute.models.model_chain_diff_request import (
    ModelChainDiffRequest,
)


def diff_chains(
    expected: tuple[ModelGoldenChainEntry, ...],
    observed: tuple[ModelGoldenChainEntry, ...],
) -> ModelChainDiff:
    """Compare expected and observed event chains without I/O or side effects."""

    expected_by_event = {entry.event_type: entry for entry in expected}
    observed_by_event = {entry.event_type: entry for entry in observed}

    missing_events = tuple(
        entry for entry in expected if entry.event_type not in observed_by_event
    )
    unexpected_events = tuple(
        entry for entry in observed if entry.event_type not in expected_by_event
    )

    order_mismatches: list[str] = []
    topic_mismatches: list[str] = []

    for observed_entry in observed:
        expected_entry = expected_by_event.get(observed_entry.event_type)
        if expected_entry is None:
            continue
        if observed_entry.sequence != expected_entry.sequence:
            order_mismatches.append(
                f"{observed_entry.event_type}: expected seq "
                f"{expected_entry.sequence}, observed seq {observed_entry.sequence}"
            )
        if observed_entry.topic != expected_entry.topic:
            topic_mismatches.append(
                f"{observed_entry.event_type}: expected topic "
                f"{expected_entry.topic!r}, observed {observed_entry.topic!r}"
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
        unexpected_events=unexpected_events,
        order_mismatches=tuple(order_mismatches),
        topic_mismatches=tuple(topic_mismatches),
    )


class HandlerChainDiff:
    """ONEX compute handler for deterministic chain comparison."""

    def handle(self, request: ModelChainDiffRequest) -> ModelChainDiff:
        return diff_chains(request.expected, request.observed)


__all__ = ["HandlerChainDiff", "diff_chains"]
