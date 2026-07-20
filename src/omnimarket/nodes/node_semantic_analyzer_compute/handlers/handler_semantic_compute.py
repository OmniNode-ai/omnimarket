# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic compute handler — canonical def-B dispatch entrypoint (OMN-14841).

The semantic-analysis business logic (embedding generation, entity extraction,
and full analysis) lives in omnimemory alongside the
``ProtocolEmbeddingProvider`` / ``ProtocolLLMProvider`` it depends on (migrated
from omnimemory under OMN-8297, Wave 1). Omnimarket registers the entry point;
omnimemory owns the ~1600-line implementation.

This module owns the canonical **definition-B** dispatch entrypoint (OMN-14355 /
OMN-12525). ``HandlerSemanticCompute`` subclasses the omnimemory handler and adds
the single typed-payload ``handle(request) -> response`` method the shared runtime
adapts (``omnibase_core.runtime.runtime_local_adapter``, OMN-8724). Before this
flip the handler was a pure re-export exposing NEITHER ``handle`` nor
``handle_async``, so the shared auto-wiring bound it to ``_missing_handle`` (raises
``ModelOnexError`` on every dispatch) — it was frozen into the shape baseline
(``phantom``) and ``validation/handler_dispatch_entrypoint_baseline.yaml``
(entrypointless, OMN-14617).

``handle`` is a verbatim port of the operation-routing in omnimemory
``NodeSemanticAnalyzerCompute.execute`` (the pre-flip dispatch surface), delegating
to the unchanged inherited ``embed`` / ``extract_entities`` / ``analyze`` compute
methods. The flip therefore preserves behavior (an equivalence flip, not a
rewrite): the core carries no runtime event-envelope type (the envelope
boundary stays in the shared runtime adapter), and no ``Plugin*`` base.
"""

from __future__ import annotations

from omnimemory.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute as _MemoryHandlerSemanticCompute,
)
from omnimemory.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticComputePolicy,
)

from omnimarket.nodes.node_semantic_analyzer_compute.models.model_semantic_analyzer_compute_request import (
    ModelSemanticAnalyzerComputeRequest,
)
from omnimarket.nodes.node_semantic_analyzer_compute.models.model_semantic_analyzer_compute_response import (
    ModelSemanticAnalyzerComputeResponse,
)

__all__ = [
    "HandlerSemanticCompute",
    "HandlerSemanticComputePolicy",
]


# NOTE(OMN-14841): omnimemory ships untyped, so mypy sees the re-exported base as
# Any; subclassing it to add only the canonical def-B handle over unchanged inherited
# compute is intentional and the sole reason for this ignore.
class HandlerSemanticCompute(_MemoryHandlerSemanticCompute):  # type: ignore[misc]
    """Canonical def-B semantic-analysis compute handler.

    Inherits the provider-backed ``embed`` / ``extract_entities`` / ``analyze``
    compute (and the container-driven lifecycle) from the omnimemory
    implementation, and exposes the single canonical dispatch entrypoint
    ``handle(request) -> response``. The routing is a verbatim port of the pre-flip
    ``NodeSemanticAnalyzerCompute.execute`` surface, so the def-B flip is
    behavior-preserving.
    """

    async def handle(
        self, request: ModelSemanticAnalyzerComputeRequest
    ) -> ModelSemanticAnalyzerComputeResponse:
        """Route a semantic-analysis request to the matching compute operation.

        Auto-initializes the inherited handler lifecycle on first dispatch, then
        dispatches on ``request.operation`` exactly as the legacy node did. Errors
        are mapped to a typed error response rather than propagated, matching the
        pre-flip ``execute`` contract.
        """
        try:
            if not self.is_initialized:
                await self.initialize()

            match request.operation:
                case "embed":
                    embedding = await self.embed(
                        content=request.content,
                        model=request.model,
                        correlation_id=request.correlation_id,
                    )
                    return ModelSemanticAnalyzerComputeResponse(
                        status="success",
                        operation="embed",
                        embedding=embedding,
                        embedding_dimension=len(embedding),
                        model_name=self.embedding_provider.model_name,
                    )

                case "extract_entities":
                    entity_list = await self.extract_entities(
                        content=request.content,
                        correlation_id=request.correlation_id,
                    )
                    return ModelSemanticAnalyzerComputeResponse(
                        status="success",
                        operation="extract_entities",
                        entities=entity_list,
                    )

                case "analyze":
                    result = await self.analyze(
                        content=request.content,
                        analysis_type=request.analysis_type,
                        correlation_id=request.correlation_id,
                    )
                    return ModelSemanticAnalyzerComputeResponse(
                        status="success",
                        operation="analyze",
                        embedding=result.semantic_vector
                        if result.semantic_vector
                        else None,
                        embedding_dimension=len(result.semantic_vector)
                        if result.semantic_vector
                        else None,
                        entities=result.entity_list,
                        topics=result.topics,
                        key_concepts=result.key_concepts,
                        confidence_score=result.confidence_score,
                        complexity_score=result.complexity_score,
                        readability_score=result.readability_score,
                        result_id=result.result_id,
                        model_name=result.model_name,
                        processing_time_ms=result.processing_time_ms,
                    )

                case _:
                    # Defensive: Pydantic validates the Literal, but a
                    # deserialization bypass would land here.
                    return ModelSemanticAnalyzerComputeResponse(
                        status="error",
                        operation=request.operation,
                        error_message=f"Unknown operation: {request.operation}",
                    )

        except ValueError as exc:
            return ModelSemanticAnalyzerComputeResponse(
                status="error",
                operation=request.operation,
                error_message=str(exc),
            )
        except Exception as exc:
            return ModelSemanticAnalyzerComputeResponse(
                status="error",
                operation=request.operation,
                error_message=f"Unexpected error: {type(exc).__name__}: {exc}",
            )
