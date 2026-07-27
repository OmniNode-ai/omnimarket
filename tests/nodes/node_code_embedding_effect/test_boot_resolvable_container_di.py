# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Container-driven DI proof for HandlerCodeEmbeddingEffect [OMN-15228].

Three properties are proven here, all against real artifacts:

1. **Boot-resolvability, through the real gate helper.**
   ``_required_non_injectable_params`` is imported from
   ``tests/test_handler_routing_boot_resolvable.py`` — the OMN-13551 guard that
   backs the ``Coverage Sweep Gate`` CI job — and applied to the real handler
   class. No surrogate class, no reimplementation of the predicate. Before the
   OMN-15228 refactor it returned ``['repository']``; after it returns ``[]``.

   The gate test itself cannot prove this today. It only walks handlers reachable
   through ``handlers[].handler.{name,module}``, and this node's ``contract.yaml``
   is still on the flat ``handler_class``/``handler_module`` schema, so the class
   is invisible to it and the gate is *vacuously green*. Converting the contract
   to the nested shape is the sequential OMN-15027 slice; until it lands, this
   assertion is what holds the constructor shape.

2. **Behavior preservation** — the batch embedding path produces the same
   observable effects when the repository arrives via the container instead of a
   ``repository=`` constructor kwarg.

3. **Fail-loud on a missing provider** — a real ``ModelONEXContainer`` with
   nothing registered propagates out of ``handle()`` instead of degrading to a
   zero-count result.

Placed under ``tests/`` rather than the node-local
``src/omnimarket/nodes/node_code_embedding_effect/tests/`` because CI collects
``pytest tests/`` and the governed selector (``scripts/ci/detect_test_paths.py``)
maps changed sources only to ``tests/`` paths — a proof living under ``src/``
would never run in CI.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.container import ModelONEXContainer
from omnibase_core.models.errors.model_onex_error import ModelOnexError

from omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect import (
    HandlerCodeEmbeddingEffect,
    ProtocolCodeEntityRepository,
)
from omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_request import (
    ModelCodeEmbeddingRequest,
)
from omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_result import (
    ModelCodeEmbeddingResult,
)

# The real OMN-13551 gate predicate — imported, never reimplemented.
from tests.test_handler_routing_boot_resolvable import (
    _KNOWN_INJECTABLE,
    _required_non_injectable_params,
)

_ENDPOINT = "http://test-embed:8100"


def _make_entity(**overrides: Any) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "id": overrides.pop("id", str(uuid4())),
        "entity_name": "MyClass",
        "entity_type": "class",
        "qualified_name": "mypackage.mymodule.MyClass",
        "source_repo": "omniintelligence",
        "source_path": "src/omniintelligence/models/my_class.py",
        "docstring": "A sample class for testing.",
        "signature": "class MyClass(BaseModel):",
        "classification": "model",
        "llm_description": None,
    }
    entity.update(overrides)
    return entity


def _make_mock_repository(entities: list[dict[str, Any]]) -> MagicMock:
    repo = MagicMock()
    repo.get_entities_needing_embedding = AsyncMock(return_value=entities)
    repo.update_embedded_at = AsyncMock()
    return repo


def _make_mock_qdrant() -> MagicMock:
    qdrant = MagicMock()
    qdrant.get_collections.return_value = MagicMock(collections=[])
    qdrant.upsert = MagicMock()
    return qdrant


def _container_providing(repository: MagicMock) -> MagicMock:
    """Container double whose ``get_service`` yields *repository*.

    Mirrors the in-repo OMN-13603 precedent (``_handler_with_tracker`` in
    ``tests/test_golden_chain_ticket_query.py``). A real ``ModelONEXContainer``
    cannot stand in here: its ``get_service`` dispatches on a hardcoded set of
    protocol names (``ProtocolEventBus``, ``ProtocolVaultClient``, the named
    registries) and has no generic registration path for an arbitrary protocol
    key, so it can only ever raise for ``ProtocolCodeEntityRepository``. That is
    exactly what ``test_missing_provider_fails_loud`` asserts against the real
    container; provisioning a resolvable production repository is out of scope
    here and tracked in OMN-15230.
    """
    container = MagicMock()
    container.get_service.return_value = repository
    return container


def _patched_embedding_client(embedding: list[float]) -> Any:
    """Patch httpx.AsyncClient so /v1/embeddings returns *embedding*."""
    ctx = patch(
        "omnimarket.nodes.node_code_embedding_effect.handlers."
        "handler_code_embedding_effect.httpx.AsyncClient"
    )
    mock_client_cls = ctx.__enter__()
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": embedding}]}
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_client_cls.return_value = mock_http
    return ctx


# =============================================================================
# 1. Boot resolvability — driven by the real gate predicate
# =============================================================================


@pytest.mark.unit
class TestBootResolvable:
    def test_ctor_requires_no_non_injectable_params(self) -> None:
        """The OMN-13551 predicate reports nothing unresolvable for this class.

        RED before OMN-15228 (``['repository']``), GREEN after. This is the
        durable stand-in for the gate test, which cannot see this handler while
        the contract still uses the flat ``handler_class`` schema.
        """
        assert _required_non_injectable_params(HandlerCodeEmbeddingEffect) == []

    def test_ctor_actually_takes_the_injectable_container(self) -> None:
        """Guard against passing test 1 by having no constructor at all."""
        params = inspect.signature(HandlerCodeEmbeddingEffect).parameters
        assert "container" in params, (
            "HandlerCodeEmbeddingEffect must take the injectable `container` "
            f"param; got {list(params)}"
        )
        assert "container" in _KNOWN_INJECTABLE
        assert params["container"].default is inspect.Parameter.empty

    def test_handle_keeps_the_definition_b_signature(self) -> None:
        """Definition-B is unchanged: one typed payload in, one typed model out.

        Guards against copying HandlerTicketQuery's two-param
        ``handle(correlation_id, input_data)`` form, which would trip the
        OMN-14355 canon-shape ratchet.
        """
        sig = inspect.signature(HandlerCodeEmbeddingEffect.handle)
        params = [p for p in sig.parameters if p != "self"]
        assert params == ["payload"]
        assert (
            sig.parameters["payload"].annotation is ModelCodeEmbeddingRequest
            or sig.parameters["payload"].annotation == "ModelCodeEmbeddingRequest"
        )
        assert (
            sig.return_annotation is ModelCodeEmbeddingResult
            or sig.return_annotation == "ModelCodeEmbeddingResult"
        )


# =============================================================================
# 2. Behavior preservation through the container-resolved repository
# =============================================================================


@pytest.mark.unit
class TestContainerResolvedBehavior:
    @pytest.mark.asyncio
    async def test_embeds_batch_through_container_resolved_repository(self) -> None:
        """Same observable effects the ``repository=`` kwarg produced before."""
        entity_id = str(uuid4())
        repo = _make_mock_repository([_make_entity(id=entity_id)])
        qdrant = _make_mock_qdrant()
        fake_embedding = [0.1] * 4096
        handler = HandlerCodeEmbeddingEffect(
            container=_container_providing(repo), qdrant_client=qdrant
        )

        ctx = _patched_embedding_client(fake_embedding)
        try:
            result = await handler.handle(
                ModelCodeEmbeddingRequest(
                    correlation_id="omn-15228-behavior",
                    embedding_endpoint_override=_ENDPOINT,
                )
            )
        finally:
            ctx.__exit__(None, None, None)

        assert result.embedded_count == 1
        assert result.failed_count == 0
        assert result.correlation_id == "omn-15228-behavior"

        point = qdrant.upsert.call_args.kwargs["points"][0]
        assert point.id == entity_id
        assert point.vector == fake_embedding
        repo.update_embedded_at.assert_awaited_once_with([entity_id])

    @pytest.mark.asyncio
    async def test_repository_is_resolved_by_protocol_key(self) -> None:
        """The container is asked for the protocol, not a string or a concrete type."""
        repo = _make_mock_repository([])
        container = _container_providing(repo)
        handler = HandlerCodeEmbeddingEffect(
            container=container, qdrant_client=_make_mock_qdrant()
        )

        await handler.handle(
            ModelCodeEmbeddingRequest(
                correlation_id="omn-15228-key",
                embedding_endpoint_override=_ENDPOINT,
            )
        )

        container.get_service.assert_called_once_with(ProtocolCodeEntityRepository)

    @pytest.mark.asyncio
    async def test_repository_not_touched_at_construction(self) -> None:
        """Construction resolves nothing — that is what makes boot safe."""
        container = _container_providing(_make_mock_repository([]))
        HandlerCodeEmbeddingEffect(container=container)
        container.get_service.assert_not_called()


# =============================================================================
# 3. Missing provider fails loud
# =============================================================================


@pytest.mark.unit
class TestMissingProviderFailsLoud:
    @pytest.mark.asyncio
    async def test_missing_provider_fails_loud(self) -> None:
        """A real container with no repository registered raises out of handle().

        Not an empty ``ModelCodeEmbeddingResult``, not ``None`` — a wiring gap
        must be visible to the caller.
        """
        handler = HandlerCodeEmbeddingEffect(
            container=ModelONEXContainer(), qdrant_client=_make_mock_qdrant()
        )

        with pytest.raises(ModelOnexError):
            await handler.handle(
                ModelCodeEmbeddingRequest(
                    correlation_id="omn-15228-missing-provider",
                    embedding_endpoint_override=_ENDPOINT,
                )
            )
