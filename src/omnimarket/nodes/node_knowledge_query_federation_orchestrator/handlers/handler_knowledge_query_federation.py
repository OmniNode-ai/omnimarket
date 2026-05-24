# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for knowledge query federation — deterministic keyword-based routing.

Routing rules (no LLM dependency):
  - "dependency", "imports", "blast radius", "affects" → Memgraph
  - specific file path (contains "/" or ".py"/".ts" etc.) or "function", "class" → Repowise
  - "pattern", "smell", "antipattern", "duplicate" → Qdrant
  - ambiguous (no keyword match) → fan out to all three
"""

from __future__ import annotations

import re

from ..models.model_request import EnumKnowledgeQueryBackend
from ..models.model_response import ModelKnowledgeFederatedResult

__all__ = ["HandlerKnowledgeQueryFederation"]

# Keywords that indicate a Memgraph (graph/dependency) query
_MEMGRAPH_KEYWORDS: frozenset[str] = frozenset(
    [
        "dependency",
        "dependencies",
        "imports",
        "import",
        "blast radius",
        "affects",
        "affect",
        "depends on",
        "depend on",
        "circular",
        "cross-repo",
        "cross repo",
        "transitive",
    ]
)

# Keywords that indicate a Repowise (specific symbol/file) query.
# Only include terms that unambiguously reference a named code symbol —
# "module", "file", and "function" are too common in general prose and
# fire false positives on Memgraph/Qdrant queries.
_REPOWISE_KEYWORDS: frozenset[str] = frozenset(
    [
        "class",
        "method",
        "symbol",
    ]
)

# Regex for detecting a file path reference (contains "/" or ends with extension)
_FILE_PATH_RE = re.compile(
    r"(?:^|\s)"  # word boundary
    r"(?:[a-zA-Z0-9_\-]+/)+"  # at least one directory segment
    r"[a-zA-Z0-9_\-\.]+",  # filename
)

# Keywords that indicate a Qdrant (semantic pattern/smell) query
_QDRANT_KEYWORDS: frozenset[str] = frozenset(
    [
        "pattern",
        "patterns",
        "smell",
        "smells",
        "antipattern",
        "anti-pattern",
        "antipatterns",
        "duplicate",
        "duplicates",
        "duplication",
        "code quality",
    ]
)


class HandlerKnowledgeQueryFederation:
    """Deterministic heuristic routing and result federation handler.

    This handler is stateless and has no I/O — it only classifies queries
    and merges pre-fetched backend results. Actual backend calls are
    performed by the effect nodes this orchestrator fans out to.
    """

    def classify_backends(self, query: str) -> set[EnumKnowledgeQueryBackend]:
        """Classify which backends to query based on keyword heuristics.

        Args:
            query: Natural-language knowledge query string.

        Returns:
            Set of backends to query. Returns all three when ambiguous.
        """
        lower = query.lower()
        backends: set[EnumKnowledgeQueryBackend] = set()

        # Memgraph: dependency/graph keywords
        if any(kw in lower for kw in _MEMGRAPH_KEYWORDS):
            backends.add(EnumKnowledgeQueryBackend.MEMGRAPH)

        # Repowise: file path pattern or symbol keywords
        if _FILE_PATH_RE.search(query) or any(kw in lower for kw in _REPOWISE_KEYWORDS):
            backends.add(EnumKnowledgeQueryBackend.REPOWISE)

        # Qdrant: code quality / pattern smell keywords
        if any(kw in lower for kw in _QDRANT_KEYWORDS):
            backends.add(EnumKnowledgeQueryBackend.QDRANT)

        # Ambiguous — fan out to all three
        if not backends:
            backends = {
                EnumKnowledgeQueryBackend.MEMGRAPH,
                EnumKnowledgeQueryBackend.REPOWISE,
                EnumKnowledgeQueryBackend.QDRANT,
            }

        return backends

    def merge_results(
        self,
        backend_results: dict[EnumKnowledgeQueryBackend, list[str]],
    ) -> list[ModelKnowledgeFederatedResult]:
        """Merge results from multiple backends, deduplicating by content hash.

        Results from each backend are converted to ModelKnowledgeFederatedResult
        with provenance tags. Duplicates (same content hash) are collapsed,
        keeping the first occurrence encountered (in backend enum order).

        Args:
            backend_results: Mapping from backend to list of result strings.

        Returns:
            Deduplicated list of federated results with provenance and rank.
        """
        seen_hashes: set[str] = set()
        merged: list[ModelKnowledgeFederatedResult] = []
        rank = 0

        for backend in EnumKnowledgeQueryBackend:
            for content in backend_results.get(backend, []):
                result = ModelKnowledgeFederatedResult(
                    content=content,
                    source=backend,
                    rank=rank,
                )
                if result.content_hash not in seen_hashes:
                    seen_hashes.add(result.content_hash)
                    merged.append(result)
                    rank += 1

        return merged
