"""Tests for deterministic golden-chain diffing."""

from __future__ import annotations

import pytest
from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry

from omnimarket.nodes.node_chain_diff_compute.handlers.handler_chain_diff import (
    HandlerChainDiff,
)
from omnimarket.nodes.node_chain_diff_compute.models.model_chain_diff_request import (
    ModelChainDiffRequest,
)


def _entry(
    sequence: int,
    event_type: str,
    topic: str | None = None,
    source_node: str = "node_test",
) -> ModelGoldenChainEntry:
    return ModelGoldenChainEntry(
        sequence=sequence,
        event_type=event_type,
        topic=topic or f"onex.evt.omnimarket.{event_type}.v1",
        source_node=source_node,
    )


@pytest.mark.unit
class TestHandlerChainDiff:
    def test_exact_match(self) -> None:
        entries = (_entry(1, "started"), _entry(2, "completed"))
        result = HandlerChainDiff().handle(
            ModelChainDiffRequest(expected=entries, observed=entries)
        )

        assert result.matches is True
        assert result.expected_count == 2
        assert result.observed_count == 2
        assert result.missing_events == ()
        assert result.unexpected_events == ()

    def test_missing_event(self) -> None:
        expected = (_entry(1, "started"), _entry(2, "completed"))
        observed = (_entry(1, "started"),)

        result = HandlerChainDiff().handle(
            ModelChainDiffRequest(expected=expected, observed=observed)
        )

        assert result.matches is False
        assert result.missing_events == (expected[1],)

    def test_unexpected_event(self) -> None:
        expected = (_entry(1, "started"),)
        observed = (_entry(1, "started"), _entry(2, "extra"))

        result = HandlerChainDiff().handle(
            ModelChainDiffRequest(expected=expected, observed=observed)
        )

        assert result.matches is False
        assert result.unexpected_events == (observed[1],)

    def test_order_mismatch(self) -> None:
        expected = (_entry(1, "started"),)
        observed = (_entry(2, "started"),)

        result = HandlerChainDiff().handle(
            ModelChainDiffRequest(expected=expected, observed=observed)
        )

        assert result.matches is False
        assert result.order_mismatches == ("started: expected seq 1, observed seq 2",)

    def test_topic_mismatch(self) -> None:
        expected = (_entry(1, "started", "onex.evt.omnimarket.started.v1"),)
        observed = (_entry(1, "started", "onex.evt.omnimarket.other.v1"),)

        result = HandlerChainDiff().handle(
            ModelChainDiffRequest(expected=expected, observed=observed)
        )

        assert result.matches is False
        assert result.topic_mismatches == (
            "started: expected topic 'onex.evt.omnimarket.started.v1', "
            "observed 'onex.evt.omnimarket.other.v1'",
        )

    def test_repeated_invocation_is_deterministic(self) -> None:
        request = ModelChainDiffRequest(
            expected=(_entry(1, "started"), _entry(2, "completed")),
            observed=(_entry(1, "started"),),
        )
        handler = HandlerChainDiff()

        assert handler.handle(request) == handler.handle(request)
