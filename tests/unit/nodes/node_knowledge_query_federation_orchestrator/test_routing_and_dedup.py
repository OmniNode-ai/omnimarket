# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for node_knowledge_query_federation_orchestrator routing and deduplication.

Tests verify:
- Keyword-based routing classifier for each category
- Multi-backend fan-out for ambiguous queries
- Result merge with provenance tags
- Deduplication by content hash
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_knowledge_query_federation_orchestrator.handlers.handler_knowledge_query_federation import (
    HandlerKnowledgeQueryFederation,
)
from omnimarket.nodes.node_knowledge_query_federation_orchestrator.models.model_request import (
    EnumKnowledgeQueryBackend,
    ModelKnowledgeQueryFederationRequest,
)
from omnimarket.nodes.node_knowledge_query_federation_orchestrator.models.model_response import (
    ModelKnowledgeFederatedResult,
    ModelKnowledgeQueryFederationResponse,
)

# ---------------------------------------------------------------------------
# Routing classifier tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected_backends"),
    [
        # Memgraph keywords
        (
            "what does this module dependency look like?",
            {EnumKnowledgeQueryBackend.MEMGRAPH},
        ),
        ("show me all imports for this package", {EnumKnowledgeQueryBackend.MEMGRAPH}),
        (
            "what is the blast radius of changing this file?",
            {EnumKnowledgeQueryBackend.MEMGRAPH},
        ),
        (
            "what does this function affect downstream?",
            {EnumKnowledgeQueryBackend.MEMGRAPH},
        ),
        # Repowise keywords — specific file/function/class
        (
            "find the method handle_subscription in handlers/handler_foo.py",
            {EnumKnowledgeQueryBackend.REPOWISE},
        ),
        (
            "show me the class ModelAgentCoordinatorRequest",
            {EnumKnowledgeQueryBackend.REPOWISE},
        ),
        (
            "what does src/omnimarket/nodes/node_foo/__init__.py do?",
            {EnumKnowledgeQueryBackend.REPOWISE},
        ),
        # Qdrant keywords — code pattern / smell
        ("find code smell in this module", {EnumKnowledgeQueryBackend.QDRANT}),
        (
            "detect antipattern usage across handlers",
            {EnumKnowledgeQueryBackend.QDRANT},
        ),
        ("look for duplicate logic patterns", {EnumKnowledgeQueryBackend.QDRANT}),
        # Ambiguous — fan out to all three
        (
            "how does the system work overall?",
            {
                EnumKnowledgeQueryBackend.MEMGRAPH,
                EnumKnowledgeQueryBackend.REPOWISE,
                EnumKnowledgeQueryBackend.QDRANT,
            },
        ),
        (
            "explain this codebase",
            {
                EnumKnowledgeQueryBackend.MEMGRAPH,
                EnumKnowledgeQueryBackend.REPOWISE,
                EnumKnowledgeQueryBackend.QDRANT,
            },
        ),
    ],
)
def test_routing_classifier(
    query: str, expected_backends: set[EnumKnowledgeQueryBackend]
) -> None:
    """Keyword-based routing sends queries to the correct backends."""
    handler = HandlerKnowledgeQueryFederation()
    actual = handler.classify_backends(query)
    assert actual == expected_backends, (
        f"Query {query!r} routed to {actual}, expected {expected_backends}"
    )


@pytest.mark.unit
def test_routing_dependency_keyword() -> None:
    """'dependency' keyword routes to Memgraph only."""
    handler = HandlerKnowledgeQueryFederation()
    result = handler.classify_backends("show me the dependency graph for omnimarket")
    assert result == {EnumKnowledgeQueryBackend.MEMGRAPH}


@pytest.mark.unit
def test_routing_file_path_routes_to_repowise() -> None:
    """A query containing a file path routes to Repowise."""
    handler = HandlerKnowledgeQueryFederation()
    result = handler.classify_backends("what does src/omnimarket/nodes/node_foo.py do?")
    assert result == {EnumKnowledgeQueryBackend.REPOWISE}


@pytest.mark.unit
def test_routing_smell_routes_to_qdrant() -> None:
    """'smell' keyword routes to Qdrant."""
    handler = HandlerKnowledgeQueryFederation()
    result = handler.classify_backends("identify any code smell in the handlers")
    assert result == {EnumKnowledgeQueryBackend.QDRANT}


@pytest.mark.unit
def test_routing_ambiguous_fans_out_to_all() -> None:
    """A query with no keywords fans out to all three backends."""
    handler = HandlerKnowledgeQueryFederation()
    result = handler.classify_backends("tell me something interesting")
    assert result == {
        EnumKnowledgeQueryBackend.MEMGRAPH,
        EnumKnowledgeQueryBackend.REPOWISE,
        EnumKnowledgeQueryBackend.QDRANT,
    }


# ---------------------------------------------------------------------------
# Merge and deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_merge_results_with_provenance() -> None:
    """Merged results carry provenance tags identifying their source backend."""
    handler = HandlerKnowledgeQueryFederation()
    backend_results: dict[EnumKnowledgeQueryBackend, list[str]] = {
        EnumKnowledgeQueryBackend.MEMGRAPH: ["node A depends on node B"],
        EnumKnowledgeQueryBackend.REPOWISE: ["handler_foo.py defines class Foo"],
    }
    merged = handler.merge_results(backend_results)
    sources = {r.source for r in merged}
    assert EnumKnowledgeQueryBackend.MEMGRAPH in sources
    assert EnumKnowledgeQueryBackend.REPOWISE in sources


@pytest.mark.unit
def test_deduplication_removes_identical_content() -> None:
    """Identical content from two backends appears only once in the merged output."""
    handler = HandlerKnowledgeQueryFederation()
    duplicate_text = "module foo imports module bar"
    backend_results: dict[EnumKnowledgeQueryBackend, list[str]] = {
        EnumKnowledgeQueryBackend.MEMGRAPH: [duplicate_text],
        EnumKnowledgeQueryBackend.REPOWISE: [duplicate_text],
    }
    merged = handler.merge_results(backend_results)
    contents = [r.content for r in merged]
    assert contents.count(duplicate_text) == 1, (
        f"Duplicate content should appear once; got {contents}"
    )


@pytest.mark.unit
def test_deduplication_keeps_unique_content() -> None:
    """Unique content from each backend is preserved after deduplication."""
    handler = HandlerKnowledgeQueryFederation()
    backend_results: dict[EnumKnowledgeQueryBackend, list[str]] = {
        EnumKnowledgeQueryBackend.MEMGRAPH: ["graph result A"],
        EnumKnowledgeQueryBackend.REPOWISE: ["repowise result B"],
        EnumKnowledgeQueryBackend.QDRANT: ["qdrant result C"],
    }
    merged = handler.merge_results(backend_results)
    assert len(merged) == 3


@pytest.mark.unit
def test_content_hash_used_for_deduplication() -> None:
    """Content hash field on ModelKnowledgeFederatedResult drives dedup."""
    result_a = ModelKnowledgeFederatedResult(
        content="same text",
        source=EnumKnowledgeQueryBackend.MEMGRAPH,
    )
    result_b = ModelKnowledgeFederatedResult(
        content="same text",
        source=EnumKnowledgeQueryBackend.REPOWISE,
    )
    assert result_a.content_hash == result_b.content_hash, (
        "Same content must produce the same content hash"
    )


@pytest.mark.unit
def test_different_content_produces_different_hash() -> None:
    """Different content text produces different content hashes."""
    result_a = ModelKnowledgeFederatedResult(
        content="text one",
        source=EnumKnowledgeQueryBackend.MEMGRAPH,
    )
    result_b = ModelKnowledgeFederatedResult(
        content="text two",
        source=EnumKnowledgeQueryBackend.MEMGRAPH,
    )
    assert result_a.content_hash != result_b.content_hash


# ---------------------------------------------------------------------------
# Model construction tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_model_construction() -> None:
    """ModelKnowledgeQueryFederationRequest can be constructed with a query."""
    req = ModelKnowledgeQueryFederationRequest(query="what depends on node_foo?")
    assert req.query == "what depends on node_foo?"


@pytest.mark.unit
def test_response_model_construction() -> None:
    """ModelKnowledgeQueryFederationResponse carries results list."""
    results = [
        ModelKnowledgeFederatedResult(
            content="node_foo depends on node_bar",
            source=EnumKnowledgeQueryBackend.MEMGRAPH,
        )
    ]
    resp = ModelKnowledgeQueryFederationResponse(
        query="what depends on node_foo?",
        results=results,
        backends_queried=[EnumKnowledgeQueryBackend.MEMGRAPH],
    )
    assert len(resp.results) == 1
    assert resp.results[0].source == EnumKnowledgeQueryBackend.MEMGRAPH
