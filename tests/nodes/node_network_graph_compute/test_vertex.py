# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N1.2: every node contract is a vertex keyed repo/name."""

from __future__ import annotations

import pytest

from omnimarket.models.network_graph import (
    EnumVertexCause,
    EnumVertexState,
    ModelContractText,
    ModelNetworkGraphVertex,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

from .support import PUBLISH_TOPIC, bus_text, contract, node_text, request

pytestmark = pytest.mark.unit


def _vertices(*contracts: ModelContractText) -> tuple[ModelNetworkGraphVertex, ...]:
    return HandlerNetworkGraphCompute().handle(request(*contracts)).vertices


def test_vertex_same_name_in_two_repos_yields_two_vertices() -> None:
    vertices = _vertices(
        contract("repo_a", "src/a/node_x/contract.yaml", node_text("node_x")),
        contract("repo_b", "src/b/node_x/contract.yaml", node_text("node_x")),
    )
    assert [v.key for v in vertices] == ["repo_a/node_x", "repo_b/node_x"]
    assert [v.repo for v in vertices] == ["repo_a", "repo_b"]


def test_vertex_invalid_yaml_below_node_type_is_unknown_contract_unreadable() -> None:
    text = "name: node_broken\nnode_type: effect\nevent_bus: [unclosed\n  - : :\n"
    (vertex,) = _vertices(contract("repo_a", "n/node_broken/contract.yaml", text))
    assert vertex.key == "repo_a/node_broken"
    assert vertex.state is EnumVertexState.UNKNOWN
    assert vertex.cause is EnumVertexCause.CONTRACT_UNREADABLE
    assert vertex.publishes == ()
    assert vertex.subscribes == ()


def test_vertex_unreadable_without_a_name_is_named_by_its_path() -> None:
    text = "node_type: effect\nevent_bus: [unclosed\n"
    (vertex,) = _vertices(contract("repo_a", "n/anon/contract.yaml", text))
    assert vertex.key == "repo_a/n/anon/contract.yaml"
    assert vertex.name == "n/anon/contract.yaml"
    assert vertex.cause is EnumVertexCause.CONTRACT_UNREADABLE


def test_vertex_a_contract_without_node_type_yields_none() -> None:
    assert (
        _vertices(
            contract("repo_a", "n/x/contract.yaml", "name: node_x\ndescription: d\n"),
            contract("repo_a", "n/y/contract.yaml", "# node_type: compute\nname: y\n"),
            contract("repo_a", "n/z/contract.yaml", "meta:\n  node_type: compute\n"),
        )
        == ()
    )


def test_vertex_a_parseable_contract_without_name_is_named_by_its_path() -> None:
    (vertex,) = _vertices(
        contract("repo_a", "n/anon/contract.yaml", "node_type: compute\n")
    )
    assert vertex.key == "repo_a/n/anon/contract.yaml"
    assert vertex.state is None
    assert vertex.cause is None


def test_vertex_name_is_found_by_text_search_with_quotes_and_comment() -> None:
    (vertex,) = _vertices(
        contract(
            "repo_a",
            "n/q/contract.yaml",
            'name: "node_quoted"  # the name\nnode_type: compute\n',
        )
    )
    assert vertex.name == "node_quoted"


def test_vertex_carries_node_type_path_and_sorted_topics() -> None:
    (vertex,) = _vertices(
        contract(
            "repo_a",
            "n/x/contract.yaml",
            bus_text("node_x", ("t.b", "t.a", "t.a"), ("t.z",)),
        )
    )
    assert vertex.path == "n/x/contract.yaml"
    assert vertex.node_type == "compute"
    assert vertex.publishes == ("t.a", "t.b")
    assert vertex.subscribes == ("t.z",)


def test_vertex_two_contracts_with_one_repo_and_name_are_told_apart_by_path() -> None:
    vertices = _vertices(
        contract("repo_a", "n/one/contract.yaml", node_text("node_dup")),
        contract("repo_a", "n/two/contract.yaml", node_text("node_dup")),
    )
    assert [v.key for v in vertices] == [
        "repo_a/n/one/contract.yaml",
        "repo_a/n/two/contract.yaml",
    ]


def test_vertex_list_is_sorted_by_key_whatever_the_input_order() -> None:
    texts = [
        contract(
            "repo_b", "b/contract.yaml", node_text("node_b", f"x: {PUBLISH_TOPIC}\n")
        ),
        contract("repo_a", "z/contract.yaml", node_text("node_z")),
        contract("repo_a", "a/contract.yaml", node_text("node_a")),
    ]
    forward = _vertices(*texts)
    backward = _vertices(*reversed(texts))
    assert [v.key for v in forward] == [
        "repo_a/node_a",
        "repo_a/node_z",
        "repo_b/node_b",
    ]
    assert forward == backward


def test_vertex_quoted_name_keeps_a_hash_inside_the_quotes() -> None:
    (vertex,) = _vertices(
        contract(
            "repo_a", "n/q/contract.yaml", 'name: "a # b"  # note\nnode_type: compute\n'
        )
    )
    assert vertex.name == "a # b"


def test_vertex_a_contract_the_loader_raises_on_is_unreadable_not_fatal() -> None:
    text = "name: node_bad\nnode_type: compute\nupdated: 2026-99-99\n"
    (vertex,) = _vertices(contract("repo_a", "n/bad/contract.yaml", text))
    assert vertex.key == "repo_a/node_bad"
    assert vertex.cause is EnumVertexCause.CONTRACT_UNREADABLE
