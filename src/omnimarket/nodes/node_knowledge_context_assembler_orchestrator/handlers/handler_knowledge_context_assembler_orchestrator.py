# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerKnowledgeContextAssemblerOrchestrator — fans out to knowledge backends in parallel.

Fan-out targets:
  L2: codebase_intelligence, antipattern_match, agent_learning_retrieval
  L3: + architecture_graph

Graceful degradation: any backend failure is recorded as an error fragment.
The reducer assembles the final bundle from all fragments (success + error).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Protocol, runtime_checkable

from omnimarket.events.knowledge_context import (
    EnumBundleStatus,
    EnumFragmentSource,
    ModelKnowledgeContextBundle,
    ModelKnowledgeContextFragment,
    ModelKnowledgeContextState,
)
from omnimarket.nodes.node_knowledge_context_assembler_orchestrator.models.model_knowledge_context_orchestrator_result import (
    ModelKnowledgeContextOrchestratorResult,
    OrchestratorStatus,
)
from omnimarket.nodes.node_knowledge_context_assembler_orchestrator.models.model_knowledge_context_request import (
    EnumContextLevel,
    ModelKnowledgeContextRequest,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.handlers.handler_knowledge_context_assembler_reducer import (
    HandlerKnowledgeContextAssemblerReducer,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HandlerKnowledgeContextAssemblerOrchestrator",
    "ProtocolKnowledgeBackend",
]


@runtime_checkable
class ProtocolKnowledgeBackend(Protocol):
    """Protocol for injectable knowledge backend effect handlers."""

    async def fetch(self, request: Any) -> dict[str, Any]:
        """Fetch context data and return a raw payload dict."""
        ...


def _status_from_bundle(bundle: ModelKnowledgeContextBundle) -> OrchestratorStatus:
    if bundle.status == EnumBundleStatus.COMPLETE:
        return "COMPLETE"
    if bundle.status == EnumBundleStatus.DEGRADED:
        return "DEGRADED"
    return "PARTIAL"


class HandlerKnowledgeContextAssemblerOrchestrator:
    """Fans out to knowledge backends in parallel and reduces responses into a bundle."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["orchestrator"] = "orchestrator"

    def __init__(
        self,
        codebase_intelligence_backend: ProtocolKnowledgeBackend,
        antipattern_backend: ProtocolKnowledgeBackend,
        agent_learning_backend: ProtocolKnowledgeBackend,
        arch_graph_backend: ProtocolKnowledgeBackend,
    ) -> None:
        self._codebase = codebase_intelligence_backend
        self._antipattern = antipattern_backend
        self._learning = agent_learning_backend
        self._arch_graph = arch_graph_backend
        self._reducer = HandlerKnowledgeContextAssemblerReducer()

    async def handle(
        self, request: ModelKnowledgeContextRequest
    ) -> ModelKnowledgeContextOrchestratorResult:
        backends: list[tuple[EnumFragmentSource, ProtocolKnowledgeBackend]] = [
            (EnumFragmentSource.CODEBASE_INTELLIGENCE, self._codebase),
            (EnumFragmentSource.ANTIPATTERN_MATCH, self._antipattern),
            (EnumFragmentSource.AGENT_LEARNING_RETRIEVAL, self._learning),
        ]
        if request.level == EnumContextLevel.L3:
            backends.append((EnumFragmentSource.ARCHITECTURE_GRAPH, self._arch_graph))

        expected_count = len(backends)

        # Fan out all backend calls in parallel
        raw_results = await asyncio.gather(
            *(
                self._call_backend(source, backend, request)
                for source, backend in backends
            ),
            return_exceptions=True,
        )

        # Build initial state and accumulate all fragments
        state = ModelKnowledgeContextState(
            correlation_id=request.correlation_id,
            expected_count=expected_count,
        )
        for (source, _), result in zip(backends, raw_results, strict=True):
            if isinstance(result, BaseException):
                fragment = ModelKnowledgeContextFragment(
                    fragment_source=source,
                    content={},
                    correlation_id=request.correlation_id,
                    error=str(result),
                )
            else:
                fragment = ModelKnowledgeContextFragment(
                    fragment_source=source,
                    content=result,
                    correlation_id=request.correlation_id,
                )
            state = self._reducer.accumulate(state, fragment)

        bundle = self._reducer.materialize(state)

        # Should always be non-None since expected_count == len(results)
        if bundle is None:
            bundle = ModelKnowledgeContextBundle(
                correlation_id=request.correlation_id,
                status=EnumBundleStatus.DEGRADED,
                fragments=state.fragments,
                fragment_count=len(state.fragments),
            )

        succeeded = sum(1 for f in bundle.fragments if f.ok)
        failed = bundle.fragment_count - succeeded

        return ModelKnowledgeContextOrchestratorResult(
            correlation_id=request.correlation_id,
            status=_status_from_bundle(bundle),
            bundle=bundle,
            succeeded_backend_count=succeeded,
            failed_backend_count=failed,
        )

    async def _call_backend(
        self,
        source: EnumFragmentSource,
        backend: ProtocolKnowledgeBackend,
        request: ModelKnowledgeContextRequest,
    ) -> dict[str, Any]:
        try:
            return await backend.fetch(request)
        except Exception as exc:
            logger.warning(
                "Backend %s failed for correlation_id=%s: %s",
                source,
                request.correlation_id,
                exc,
            )
            raise
