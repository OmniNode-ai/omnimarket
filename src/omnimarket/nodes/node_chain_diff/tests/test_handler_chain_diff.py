# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerChainDiff — all five diff scenarios plus determinism."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_chain_diff.handlers.handler_chain_diff import (
    HandlerChainDiff,
)
from omnimarket.nodes.node_chain_diff.models.model_chain_diff_request import (
    ModelChainDiffRequest,
)
from omnimarket.nodes.node_chain_diff.models.model_golden_chain_entry import (
    ModelGoldenChainEntry,
)

_TOPIC_A = "onex.evt.omnimarket.chain-diff-requested.v1"
_TOPIC_B = "onex.evt.omnimarket.chain-diff-completed.v1"
_TOPIC_C = "onex.cmd.omnimarket.closeout-verify-requested.v1"


def _entry(
    sequence: int,
    event_type: str,
    topic: str = _TOPIC_A,
    source_node: str = "node_chain_diff",
) -> ModelGoldenChainEntry:
    return ModelGoldenChainEntry(
        sequence=sequence,
        event_type=event_type,
        topic=topic,
        source_node=source_node,
    )


@pytest.mark.unit
class TestHandlerChainDiffExactMatch:
    def test_empty_chains_match(self) -> None:
        request = ModelChainDiffRequest(expected=(), observed=())
        result = HandlerChainDiff().handle(request)
        assert result.matches is True
        assert result.expected_count == 0
        assert result.observed_count == 0
        assert result.missing_events == ()
        assert result.unexpected_events == ()
        assert result.order_mismatches == ()
        assert result.topic_mismatches == ()

    def test_single_entry_exact_match(self) -> None:
        entry = _entry(1, "EventA")
        request = ModelChainDiffRequest(expected=(entry,), observed=(entry,))
        result = HandlerChainDiff().handle(request)
        assert result.matches is True
        assert result.expected_count == 1
        assert result.observed_count == 1

    def test_multi_entry_exact_match(self) -> None:
        chain = (
            _entry(1, "EventA", _TOPIC_A),
            _entry(2, "EventB", _TOPIC_B),
            _entry(3, "EventC", _TOPIC_C),
        )
        request = ModelChainDiffRequest(expected=chain, observed=chain)
        result = HandlerChainDiff().handle(request)
        assert result.matches is True
        assert not result.missing_events
        assert not result.unexpected_events
        assert not result.order_mismatches
        assert not result.topic_mismatches


@pytest.mark.unit
class TestHandlerChainDiffMissingEvent:
    def test_missing_event_detected(self) -> None:
        expected = (
            _entry(1, "EventA"),
            _entry(2, "EventB"),
        )
        observed = (_entry(1, "EventA"),)
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.missing_events) == 1
        assert result.missing_events[0].event_type == "EventB"
        assert result.unexpected_events == ()

    def test_all_expected_missing(self) -> None:
        expected = (_entry(1, "EventA"), _entry(2, "EventB"))
        request = ModelChainDiffRequest(expected=expected, observed=())
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert result.expected_count == 2
        assert result.observed_count == 0
        assert len(result.missing_events) == 2


@pytest.mark.unit
class TestHandlerChainDiffUnexpectedEvent:
    def test_unexpected_event_detected(self) -> None:
        expected = (_entry(1, "EventA"),)
        observed = (_entry(1, "EventA"), _entry(2, "EventX"))
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert result.unexpected_events != ()
        assert result.unexpected_events[0].event_type == "EventX"
        assert result.missing_events == ()

    def test_entirely_unexpected_chain(self) -> None:
        expected = (_entry(1, "EventA"),)
        observed = (_entry(1, "EventX"), _entry(2, "EventY"))
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.missing_events) == 1
        assert len(result.unexpected_events) == 2


@pytest.mark.unit
class TestHandlerChainDiffOrderMismatch:
    def test_order_mismatch_detected(self) -> None:
        expected = (
            _entry(1, "EventA"),
            _entry(2, "EventB"),
        )
        # same events, swapped sequence numbers
        observed = (
            _entry(2, "EventA"),
            _entry(1, "EventB"),
        )
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.order_mismatches) == 2
        assert result.missing_events == ()
        assert result.unexpected_events == ()

    def test_single_order_mismatch(self) -> None:
        expected = (_entry(1, "EventA"), _entry(2, "EventB"))
        observed = (_entry(1, "EventA"), _entry(99, "EventB"))
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.order_mismatches) == 1
        assert "EventB" in result.order_mismatches[0]
        assert "99" in result.order_mismatches[0]


@pytest.mark.unit
class TestHandlerChainDiffTopicMismatch:
    def test_topic_mismatch_detected(self) -> None:
        expected = (_entry(1, "EventA", _TOPIC_A),)
        observed = (_entry(1, "EventA", _TOPIC_B),)
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.topic_mismatches) == 1
        assert "EventA" in result.topic_mismatches[0]
        assert _TOPIC_A in result.topic_mismatches[0]
        assert _TOPIC_B in result.topic_mismatches[0]

    def test_multiple_topic_mismatches(self) -> None:
        expected = (
            _entry(1, "EventA", _TOPIC_A),
            _entry(2, "EventB", _TOPIC_B),
        )
        observed = (
            _entry(1, "EventA", _TOPIC_C),
            _entry(2, "EventB", _TOPIC_A),
        )
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert len(result.topic_mismatches) == 2


@pytest.mark.unit
class TestHandlerChainDiffDeterminism:
    def test_same_input_produces_identical_output(self) -> None:
        chain = (
            _entry(1, "EventA", _TOPIC_A),
            _entry(2, "EventB", _TOPIC_B),
        )
        request = ModelChainDiffRequest(expected=chain, observed=chain)
        handler = HandlerChainDiff()
        result_a = handler.handle(request)
        result_b = handler.handle(request)
        assert result_a == result_b

    def test_counts_reflect_actual_chain_lengths(self) -> None:
        expected = (
            _entry(1, "EventA"),
            _entry(2, "EventB"),
            _entry(3, "EventC"),
        )
        observed = (_entry(1, "EventA"),)
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.expected_count == 3
        assert result.observed_count == 1


@pytest.mark.unit
class TestHandlerChainDiffNoHardcodedTopics:
    def test_topics_come_from_entries_not_handler(self) -> None:
        custom_topic = "onex.evt.omnimarket.custom-event.v1"
        expected = (_entry(1, "EventA", custom_topic),)
        observed = (_entry(1, "EventA", _TOPIC_B),)
        request = ModelChainDiffRequest(expected=expected, observed=observed)
        result = HandlerChainDiff().handle(request)
        assert result.matches is False
        assert custom_topic in result.topic_mismatches[0]
