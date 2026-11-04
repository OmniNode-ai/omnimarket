# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerNetworkGraphCompute: contract texts in, vertices keyed repo/name and topic edges out.

Pure: no file, clock, network or environment. The effect that lists the contract
files hands their texts and the overlay in. Every list of the document is sorted
by key, so one request always gives one set of bytes.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.models.network_graph import (
    EnumVertexCause,
    EnumVertexState,
    ModelContractText,
    ModelDeclaredTopics,
    ModelNetworkGraphDocument,
    ModelNetworkGraphEdge,
    ModelNetworkGraphRequest,
    ModelNetworkGraphVertex,
)
from omnimarket.nodes.node_network_graph_compute.handlers.contract_reader import (
    find_top_level_scalar,
    has_top_level_key,
    parse_contract,
    read_declared_topics,
)
from omnimarket.nodes.node_network_graph_compute.handlers.dialect_table import (
    DIALECT_TABLE,
)

_NODE_TYPE_KEY = "node_type"
_NAME_KEY = "name"


def _name_of(contract: ModelContractText) -> str:
    """The contract's top-level name by text search, else its path within the repo."""
    return find_top_level_scalar(contract.text, _NAME_KEY) or contract.path


def _vertex(contract: ModelContractText, name: str) -> ModelNetworkGraphVertex:
    parsed = parse_contract(contract.text)
    topics = (
        ModelDeclaredTopics()
        if parsed is None
        else read_declared_topics(parsed, DIALECT_TABLE)
    )
    unreadable = parsed is None
    return ModelNetworkGraphVertex(
        key=f"{contract.repo}/{name}",
        repo=contract.repo,
        name=name,
        path=contract.path,
        node_type=find_top_level_scalar(contract.text, _NODE_TYPE_KEY) or "",
        publishes=tuple(sorted(topics.publishes)),
        subscribes=tuple(sorted(topics.subscribes)),
        state=EnumVertexState.UNKNOWN if unreadable else None,
        cause=EnumVertexCause.CONTRACT_UNREADABLE if unreadable else None,
    )


def _edges(
    vertices: tuple[ModelNetworkGraphVertex, ...],
) -> tuple[ModelNetworkGraphEdge, ...]:
    publishers: dict[str, list[str]] = {}
    subscribers: dict[str, list[str]] = {}
    for vertex in vertices:
        for topic in vertex.publishes:
            publishers.setdefault(topic, []).append(vertex.key)
        for topic in vertex.subscribes:
            subscribers.setdefault(topic, []).append(vertex.key)
    pairs = {
        (publisher, topic, subscriber)
        for topic, keys in publishers.items()
        for publisher in keys
        for subscriber in subscribers.get(topic, ())
    }
    return tuple(
        ModelNetworkGraphEdge(publisher=p, topic=t, subscriber=s)
        for p, t, s in sorted(pairs)
    )


class HandlerNetworkGraphCompute:
    """Build the structure of the node network graph from contract texts."""

    def handle(self, request: ModelNetworkGraphRequest) -> ModelNetworkGraphDocument:
        candidates = [
            contract
            for contract in request.contracts
            if has_top_level_key(contract.text, _NODE_TYPE_KEY)
        ]
        taken = Counter((c.repo, _name_of(c)) for c in candidates)
        vertices = tuple(
            sorted(
                (
                    _vertex(
                        contract,
                        # Two contracts of one repo with one name are told apart by path.
                        _name_of(contract)
                        if taken[(contract.repo, _name_of(contract))] == 1
                        else contract.path,
                    )
                    for contract in candidates
                ),
                key=lambda vertex: (vertex.key, vertex.path),
            )
        )
        return ModelNetworkGraphDocument(
            generated_at=request.reference_time,
            overlay=request.overlay,
            vertices=vertices,
            edges=_edges(vertices),
        )
