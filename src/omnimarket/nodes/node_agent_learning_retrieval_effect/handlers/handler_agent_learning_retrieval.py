# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical handler for node_agent_learning_retrieval_effect.

Retrieves agent-learning records via an injected retrieval backend. The
backend (Qdrant similarity search + embedding client) is supplied through the
DI container as a ``ProtocolAgentLearningRetrievalBackend`` adapter; the
handler owns request shaping and response assembly only, never raw I/O.

Local-default behaviour (canonical, OMN-13713): when no backend is wired into
the container — the default over the in-memory bus — the handler degrades
honestly to an empty result (zero matches, ``query_ms=0``) rather than raising
a ``backend required`` error that would crash every local invocation. This is
the same degrade-don't-crash posture as ``node_recall_compute``: a node is only
correct if it runs over the local in-memory bus.

Before this handler existed the node's entry-point marker class
(``NodeAgentLearningRetrievalEffect``) was wired as the handler; it has no
``handle()`` method, so every dispatch failed with
``Handler ... has no handle()/run()/execute() method``.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_request import (
    ModelAgentLearningRetrievalRequest,
)
from omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_response import (
    ModelAgentLearningRetrievalResponse,
    ModelRetrievedLearning,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ProtocolAgentLearningRetrievalBackend(Protocol):
    """Adapter boundary for an agent-learning retrieval backend.

    Implementations perform the Qdrant similarity search (error-signature and
    task-context collections) and return ranked, freshness-decayed matches.
    The omnimemory Qdrant adapter is the production implementation; it is
    injected at runtime via DI and is absent over the default local bus.
    """

    async def retrieve(
        self,
        request: ModelAgentLearningRetrievalRequest,
    ) -> tuple[ModelRetrievedLearning, ...]:
        """Return ranked learning matches for *request* (most-relevant first)."""
        ...


class HandlerAgentLearningRetrieval:
    """Retrieve agent learnings via an injected backend, degrading honestly."""

    def __init__(
        self,
        backend: ProtocolAgentLearningRetrievalBackend | None = None,
    ) -> None:
        self._backend = backend

    async def handle(
        self,
        request: ModelAgentLearningRetrievalRequest,
    ) -> ModelAgentLearningRetrievalResponse:
        """Retrieve learnings for *request*.

        Returns ranked matches when a backend is wired, or an honest empty
        result (no silent fabrication) when none is configured locally.
        """
        start_ns = time.perf_counter_ns()

        if self._backend is None:
            logger.warning(
                "node_agent_learning_retrieval_effect: no retrieval backend "
                "wired into the container — returning empty result "
                "(repo=%s, match_type=%s). Inject a "
                "ProtocolAgentLearningRetrievalBackend to enable Qdrant search.",
                request.repo,
                request.match_type.value,
            )
            return ModelAgentLearningRetrievalResponse(
                matches=(),
                query_ms=_elapsed_ms(start_ns),
                error_matches_count=0,
                context_matches_count=0,
            )

        matches = await self._backend.retrieve(request)
        ranked = sorted(
            matches,
            key=lambda m: m.combined_score,
            reverse=True,
        )[: request.max_results]
        from omnimemory.nodes.node_agent_learning_retrieval_effect.models.model_request import (
            EnumRetrievalMatchType,
        )

        error_count = sum(
            1 for m in ranked if m.match_type == EnumRetrievalMatchType.ERROR_SIGNATURE
        )
        context_count = sum(
            1 for m in ranked if m.match_type == EnumRetrievalMatchType.TASK_CONTEXT
        )
        return ModelAgentLearningRetrievalResponse(
            matches=tuple(ranked),
            query_ms=_elapsed_ms(start_ns),
            error_matches_count=error_count,
            context_matches_count=context_count,
        )


def _elapsed_ms(start_ns: int) -> int:
    """Return whole milliseconds elapsed since *start_ns* (never negative)."""
    return max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)


__all__ = [
    "HandlerAgentLearningRetrieval",
    "ProtocolAgentLearningRetrievalBackend",
]
