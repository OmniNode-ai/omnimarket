# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Def-B dispatch entrypoint + equivalence proof for node_semantic_analyzer_compute.

OMN-14841 (Class-B Tier-1 canonical-shape flip). Proves:

* the contract-declared handler now exposes the canonical def-B entrypoint
  ``handle(request) -> response`` (RED on the pre-flip re-export, which exposed
  NEITHER ``handle`` nor ``handle_async`` and was bound to ``_missing_handle``);
* ``handle`` routes every operation (embed / extract_entities / analyze) to the
  inherited provider-backed compute and returns the typed response; and
* ``handle`` is behaviourally EQUIVALENT to the pre-flip routing surface
  ``omnimemory NodeSemanticAnalyzerCompute.execute`` over the same inputs and the
  same injected provider (the flip is an equivalence flip, not a rewrite).

Providers are faked (deterministic, no network) so the proof is hermetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import yaml
from omnibase_core.container import ModelONEXContainer
from omnimemory.nodes.node_semantic_analyzer_compute.node_semantic_analyzer_compute import (
    NodeSemanticAnalyzerCompute,
)

from omnimarket.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute,
)
from omnimarket.nodes.node_semantic_analyzer_compute.models.model_semantic_analyzer_compute_request import (
    ModelSemanticAnalyzerComputeRequest,
)
from omnimarket.nodes.node_semantic_analyzer_compute.models.model_semantic_analyzer_compute_response import (
    ModelSemanticAnalyzerComputeResponse,
)

# Fields whose values are non-deterministic per invocation (fresh UUID / wall time).
_VOLATILE_FIELDS = frozenset({"result_id", "processing_time_ms"})


class FakeEmbeddingProvider:
    """Deterministic, process-stable embedding provider (no network, no hash salt)."""

    def __init__(self, *, dimension: int = 8) -> None:
        self._dimension = dimension

    @property
    def provider_name(self) -> str:
        return "fake-embedding"

    @property
    def model_name(self) -> str:
        return "fake-model-v1"

    @property
    def embedding_dimension(self) -> int:
        return self._dimension

    @property
    def is_available(self) -> bool:
        return True

    async def generate_embedding(
        self,
        text: str,
        *,
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[float]:
        base = sum(ord(c) for c in text)
        return [((base + i) % 1000) / 1000.0 for i in range(self._dimension)]

    async def generate_embeddings_batch(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        return [await self.generate_embedding(t) for t in texts]

    async def health_check(self) -> bool:
        return True


def _make_handler() -> HandlerSemanticCompute:
    handler = HandlerSemanticCompute(container=ModelONEXContainer())

    async def _init() -> HandlerSemanticCompute:
        await handler.initialize(embedding_provider=FakeEmbeddingProvider())
        return handler

    return asyncio.run(_init())


def _handle(
    request: ModelSemanticAnalyzerComputeRequest,
) -> ModelSemanticAnalyzerComputeResponse:
    handler = HandlerSemanticCompute(container=ModelONEXContainer())

    async def _run() -> ModelSemanticAnalyzerComputeResponse:
        await handler.initialize(embedding_provider=FakeEmbeddingProvider())
        return await handler.handle(request)

    return asyncio.run(_run())


def _handle_after_init_mutation(
    request: ModelSemanticAnalyzerComputeRequest,
    mutate: Callable[[HandlerSemanticCompute], None],
) -> ModelSemanticAnalyzerComputeResponse:
    handler = HandlerSemanticCompute(container=ModelONEXContainer())

    async def _run() -> ModelSemanticAnalyzerComputeResponse:
        await handler.initialize(embedding_provider=FakeEmbeddingProvider())
        mutate(handler)
        return await handler.handle(request)

    return asyncio.run(_run())


def _handle_with_broken_lifecycle(
    request: ModelSemanticAnalyzerComputeRequest,
) -> ModelSemanticAnalyzerComputeResponse:
    handler = HandlerSemanticCompute(container=ModelONEXContainer())

    async def _raise_runtime_error() -> None:
        raise RuntimeError("semantic lifecycle failed")

    handler.initialize = _raise_runtime_error  # type: ignore[method-assign]

    async def _run() -> ModelSemanticAnalyzerComputeResponse:
        return await handler.handle(request)

    return asyncio.run(_run())


def _execute_legacy(
    request: ModelSemanticAnalyzerComputeRequest,
) -> ModelSemanticAnalyzerComputeResponse:
    node = NodeSemanticAnalyzerCompute(
        container=ModelONEXContainer(),
        embedding_provider=FakeEmbeddingProvider(),
    )
    return asyncio.run(node.execute(request))


# --------------------------------------------------------------------------- #
# 1. Canonical def-B entrypoint EXISTS (RED on the pre-flip re-export)
# --------------------------------------------------------------------------- #


class TestSemanticComputeDispatchEntrypoint:
    def test_handler_exposes_callable_handle(self) -> None:
        """The pre-flip re-export exposed NEITHER handle nor handle_async."""
        assert callable(getattr(HandlerSemanticCompute, "handle", None))

    def test_handle_single_typed_request_param(self) -> None:
        """def-B shape: handle takes exactly one positional payload param (+ self)."""
        import inspect

        params = list(inspect.signature(HandlerSemanticCompute.handle).parameters)
        assert params == ["self", "request"]

    def test_handle_embed_routes_and_returns_embedding(self) -> None:
        resp = _handle(
            ModelSemanticAnalyzerComputeRequest(
                operation="embed", content="Hello world"
            )
        )
        assert resp.status == "success"
        assert resp.operation == "embed"
        assert resp.embedding is not None
        assert resp.embedding_dimension == len(resp.embedding)
        assert resp.model_name == "fake-model-v1"

    def test_handle_extract_entities_routes(self) -> None:
        resp = _handle(
            ModelSemanticAnalyzerComputeRequest(
                operation="extract_entities",
                content="John works at Google in NYC.",
            )
        )
        assert resp.status == "success"
        assert resp.operation == "extract_entities"
        assert resp.entities is not None

    def test_handle_analyze_routes(self) -> None:
        resp = _handle(
            ModelSemanticAnalyzerComputeRequest(
                operation="analyze",
                content="Analyze this text for semantic insights and topics.",
            )
        )
        assert resp.status == "success"
        assert resp.operation == "analyze"
        assert resp.embedding is not None


# --------------------------------------------------------------------------- #
# 2. handle() ≡ legacy NodeSemanticAnalyzerCompute.execute() (equivalence flip)
# --------------------------------------------------------------------------- #


class TestSemanticComputeDefBEquivalence:
    def _assert_equivalent(self, request: ModelSemanticAnalyzerComputeRequest) -> None:
        new = _handle(request).model_dump()
        legacy = _execute_legacy(request).model_dump()
        for field in _VOLATILE_FIELDS:
            new.pop(field, None)
            legacy.pop(field, None)
        assert new == legacy, (
            f"def-B handle diverged from legacy execute for {request.operation}"
        )

    def test_embed_equivalent(self) -> None:
        self._assert_equivalent(
            ModelSemanticAnalyzerComputeRequest(
                operation="embed", content="The quick brown fox."
            )
        )

    def test_extract_entities_equivalent(self) -> None:
        self._assert_equivalent(
            ModelSemanticAnalyzerComputeRequest(
                operation="extract_entities",
                content="Ada Lovelace wrote the first algorithm.",
            )
        )

    def test_analyze_equivalent(self) -> None:
        self._assert_equivalent(
            ModelSemanticAnalyzerComputeRequest(
                operation="analyze",
                content="Semantic analysis blends embeddings, entities, and topics.",
            )
        )

    def test_handle_maps_provider_value_error_to_typed_error(self) -> None:
        async def _raise_value_error(
            *,
            content: str,
            model: str | None = None,
            correlation_id: UUID | None = None,
        ) -> list[float]:
            raise ValueError("embedding provider rejected content")

        def _mutate(handler: HandlerSemanticCompute) -> None:
            handler.embed = _raise_value_error  # type: ignore[method-assign]

        resp = _handle_after_init_mutation(
            ModelSemanticAnalyzerComputeRequest(
                operation="embed", content="provider failure fixture"
            ),
            _mutate,
        )

        assert resp.status == "error"
        assert resp.operation == "embed"
        assert resp.error_message == "embedding provider rejected content"

    def test_handle_maps_lifecycle_exception_to_typed_error(self) -> None:
        resp = _handle_with_broken_lifecycle(
            ModelSemanticAnalyzerComputeRequest(
                operation="analyze", content="lifecycle failure fixture"
            )
        )

        assert resp.status == "error"
        assert resp.operation == "analyze"
        assert (
            resp.error_message
            == "Unexpected error: RuntimeError: semantic lifecycle failed"
        )


# --------------------------------------------------------------------------- #
# 3. Contract output-state coverage (OMN-13781 state-coverage gate)
# --------------------------------------------------------------------------- #


class TestSemanticComputeContractOutputStates:
    _CONTRACT = (
        Path(__file__).resolve().parent.parent
        / "src/omnimarket/nodes/node_semantic_analyzer_compute/contract.yaml"
    )

    def _contract(self) -> dict:
        return yaml.safe_load(self._CONTRACT.read_text())

    def test_contract_declares_terminal_output_topics(self) -> None:
        """The def-B node still declares both terminal output states it publishes."""
        publish = self._contract()["event_bus"]["publish_topics"]
        assert "onex.evt.omnimemory.semantic-analyzer-completed.v1" in publish
        assert "onex.evt.omnimemory.semantic-analyzer-failed.v1" in publish

    def test_contract_binds_defb_handler(self) -> None:
        """The contract binds the canonical def-B HandlerSemanticCompute."""
        handler = self._contract()["handler"]
        assert handler["class"] == "HandlerSemanticCompute"
        assert handler["module"].endswith(
            "node_semantic_analyzer_compute.handlers.handler_semantic_compute"
        )
