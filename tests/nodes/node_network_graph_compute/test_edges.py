# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N1.3: edges are exact -- one per (publisher, topic, subscriber), self-loops kept."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

from .support import bus_text, contract, request

pytestmark = pytest.mark.unit

EXPECTED = {
    ("r/pub", "t.fan", "r/sub_one"),
    ("r/pub", "t.fan", "r/sub_two"),
    ("r/loop", "t.loop", "r/loop"),
}


def _edges() -> set[tuple[str, str, str]]:
    document = HandlerNetworkGraphCompute().handle(
        request(
            contract("r", "pub/contract.yaml", bus_text("pub", ("t.fan",), ())),
            contract("r", "sub_one/contract.yaml", bus_text("sub_one", (), ("t.fan",))),
            contract("r", "sub_two/contract.yaml", bus_text("sub_two", (), ("t.fan",))),
            contract(
                "r",
                "lonely/contract.yaml",
                bus_text("lonely", ("t.nobody.hears",), ("t.nobody.sends",)),
            ),
            contract(
                "r", "loop/contract.yaml", bus_text("loop", ("t.loop",), ("t.loop",))
            ),
        )
    )
    return {(e.publisher, e.topic, e.subscriber) for e in document.edges}


def test_edges_are_exactly_the_expected_tuples() -> None:
    assert _edges() == EXPECTED


def test_edges_comparison_fails_on_a_missing_edge() -> None:
    assert _edges() != EXPECTED - {("r/pub", "t.fan", "r/sub_two")}


def test_edges_comparison_fails_on_an_extra_edge() -> None:
    assert _edges() != EXPECTED | {("r/pub", "t.fan", "r/lonely")}


def test_edges_comparison_fails_on_a_reversed_edge() -> None:
    reversed_edge = {("r/sub_one", "t.fan", "r/pub")}
    assert _edges() != (EXPECTED - {("r/pub", "t.fan", "r/sub_one")}) | reversed_edge


def test_edges_node_with_no_counterpart_has_no_edge() -> None:
    assert not [e for e in _edges() if "r/lonely" in (e[0], e[2])]


def test_edges_are_sorted_and_not_repeated() -> None:
    document = HandlerNetworkGraphCompute().handle(
        request(
            contract("r", "p/contract.yaml", bus_text("p", ("t.a", "t.a"), ())),
            contract("r", "s/contract.yaml", bus_text("s", (), ("t.a",))),
        )
    )
    keys = [(e.publisher, e.topic, e.subscriber) for e in document.edges]
    assert keys == sorted(set(keys))
    assert len(keys) == 1


def test_edges_cross_repo_publisher_and_subscriber_with_the_same_name_stay_distinct() -> (
    None
):
    document = HandlerNetworkGraphCompute().handle(
        request(
            contract("a", "n/contract.yaml", bus_text("n", ("t.x",), ())),
            contract("b", "n/contract.yaml", bus_text("n", (), ("t.x",))),
        )
    )
    assert [(e.publisher, e.subscriber) for e in document.edges] == [("a/n", "b/n")]
