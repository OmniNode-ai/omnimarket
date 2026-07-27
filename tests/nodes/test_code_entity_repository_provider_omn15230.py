# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch-functionality proof for the code-entity repository provider (OMN-15230).

The OMN-15228 / OMN-15229 refactors made ``HandlerCodeEmbeddingEffect`` and
``HandlerCodeEnrichmentEffect`` *boot-resolvable*: the boot container can
construct them from injectable params alone. They were not *dispatch-functional*
— no provider was ever registered for ``ProtocolCodeEntityRepository``, so the
first real dispatch of either node raised at the effect boundary.

Boot-resolvable != dispatch-functional. This module proves the second property
directly, against the real handlers and a real ``ModelONEXContainer``:

* RED half — a container with **no** provider: each handler's ``handle()``
  raises ``ServiceResolutionError``. This is the pre-fix state, kept as an
  executable regression so removing the provider re-fails here rather than
  silently shipping non-functional nodes again.
* GREEN half — the same container after ``PluginCodeEntityRepository`` (the real
  production wiring, not a hand-registration) has run ``wire_handlers()``:
  ``get_service`` resolves and ``handle()`` runs to a populated result.

Everything the store is asked for is served by an in-process double, so the test
is hermetic. The *repository behaviour* (the SQL) is proven separately, against
real Postgres, in ``test_adapter_code_entity_repository_postgres.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from omnibase_core.container import ModelONEXContainer
from omnibase_core.errors.error_service_resolution import ServiceResolutionError
from omnibase_core.models.runtime.model_domain_plugin import ModelDomainPluginConfig

from omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect import (
    HandlerCodeEmbeddingEffect,
)
from omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_request import (
    ModelCodeEmbeddingRequest,
)
from omnimarket.nodes.node_code_enrichment_effect.handlers.handler_code_enrichment_effect import (
    HandlerCodeEnrichmentEffect,
)
from omnimarket.nodes.node_code_enrichment_effect.models.model_code_enrichment_request import (
    ModelCodeEnrichmentRequest,
)
from omnimarket.plugins.plugin_code_entity_repository import PluginCodeEntityRepository
from omnimarket.protocols.protocol_code_entity_repository import (
    ProtocolCodeEntityRepository,
)
from omnimarket.repositories.repository_code_entity_postgres import (
    RepositoryCodeEntityPostgres,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


def _entity(entity_name: str = "MyHandler") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "entity_name": entity_name,
        "entity_type": "class",
        "qualified_name": f"mymodule.{entity_name}",
        "source_repo": "omniintelligence",
        "source_path": "src/mymodule.py",
        "docstring": "Handles incoming requests.",
        "signature": f"class {entity_name}(BaseHandler)",
        "classification": None,
        "llm_description": None,
        "bases": ["BaseHandler"],
        "methods": [{"name": "handle"}],
        "fields": None,
        "decorators": None,
    }


def _repository_double(entities: list[dict[str, Any]]) -> MagicMock:
    """A double satisfying the *whole* canonical protocol, both halves."""
    repo = MagicMock()
    repo.get_entities_needing_embedding = AsyncMock(return_value=entities)
    repo.update_embedded_at = AsyncMock()
    repo.get_entities_needing_enrichment = AsyncMock(return_value=entities)
    repo.update_enrichment = AsyncMock()
    return repo


class _FakeQdrantClient:
    """Minimal Qdrant surface used by HandlerCodeEmbeddingEffect."""

    def __init__(self) -> None:
        self.upserted: list[Any] = []

    def get_collections(self) -> Any:
        collections = MagicMock()
        collections.collections = [MagicMock(name="existing")]
        collections.collections[0].name = "code_patterns"
        return collections

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        self.upserted.extend(points)


def _plugin_config(container: ModelONEXContainer) -> ModelDomainPluginConfig:
    return ModelDomainPluginConfig(
        container=container,
        event_bus=MagicMock(),  # transport-mock-ok: plugin never touches the bus; required ctor field
        correlation_id=uuid4(),
        input_topic="in",
        output_topic="out",
        consumer_group="test",
    )


async def _wired_container(repository: object) -> ModelONEXContainer:
    """Container wired by the real plugin, with *repository* as the adapter.

    The plugin's own construction path is exercised; only the adapter instance
    is swapped, so the DI key, the registration call and the lifecycle hook
    under test are all production code.
    """
    container = ModelONEXContainer()
    plugin = PluginCodeEntityRepository()
    with patch(
        "omnimarket.plugins.plugin_code_entity_repository.RepositoryCodeEntityPostgres",
        return_value=repository,
    ):
        await plugin.initialize(_plugin_config(container))
    result = await plugin.wire_handlers(_plugin_config(container))
    assert result.success, result.error_message
    assert result.services_registered == ["ProtocolCodeEntityRepository"]
    return container


def _llm_response(classification: str, confidence: float) -> httpx.Response:
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "classification": classification,
                            "confidence": confidence,
                            "description": "Handles incoming HTTP requests.",
                            "pattern": "handler",
                        }
                    )
                }
            }
        ]
    }
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "http://test/v1/chat/completions"),
    )


# --------------------------------------------------------------------------
# RED half — the pre-OMN-15230 state
# --------------------------------------------------------------------------


async def test_embedding_dispatch_fails_without_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider -> loud ServiceResolutionError, never a zero-count result."""
    monkeypatch.setenv("EMBEDDING_MODEL_URL", "http://embed.test")

    handler = HandlerCodeEmbeddingEffect(
        container=ModelONEXContainer(),
        qdrant_client=_FakeQdrantClient(),
    )
    with pytest.raises(ServiceResolutionError, match="ProtocolCodeEntityRepository"):
        await handler.handle(ModelCodeEmbeddingRequest(correlation_id=str(uuid4())))


async def test_enrichment_dispatch_fails_without_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider -> loud ServiceResolutionError, never a zero-count result."""
    monkeypatch.setenv("LLM_CODER_URL", "http://llm.test")

    handler = HandlerCodeEnrichmentEffect(container=ModelONEXContainer())
    with pytest.raises(ServiceResolutionError, match="ProtocolCodeEntityRepository"):
        await handler.handle(ModelCodeEnrichmentRequest(correlation_id=str(uuid4())))


# --------------------------------------------------------------------------
# GREEN half — after the plugin registers the provider
# --------------------------------------------------------------------------


async def test_plugin_registration_makes_embedding_dispatch_functional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After wire_handlers(), a real dispatch reaches the store and embeds."""
    monkeypatch.setenv("EMBEDDING_MODEL_URL", "http://embed.test")

    entities = [_entity("Alpha"), _entity("Beta")]
    repository = _repository_double(entities)
    container = await _wired_container(repository)
    qdrant = _FakeQdrantClient()

    async def _fake_embedding(*_args: Any, **_kwargs: Any) -> list[float]:
        return [0.1, 0.2, 0.3]

    with patch(
        "omnimarket.nodes.node_code_embedding_effect.handlers."
        "handler_code_embedding_effect._get_embedding",
        new=_fake_embedding,
    ):
        handler = HandlerCodeEmbeddingEffect(container=container, qdrant_client=qdrant)
        result = await handler.handle(
            ModelCodeEmbeddingRequest(correlation_id=str(uuid4()), batch_size=10)
        )

    assert result.embedded_count == 2
    assert result.failed_count == 0
    assert len(qdrant.upserted) == 2
    repository.get_entities_needing_embedding.assert_awaited_once_with(limit=10)
    repository.update_embedded_at.assert_awaited_once_with(
        [str(entities[0]["id"]), str(entities[1]["id"])]
    )


async def test_plugin_registration_makes_enrichment_dispatch_functional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After wire_handlers(), a real dispatch reaches the store and enriches."""
    monkeypatch.setenv("LLM_CODER_URL", "http://llm.test")
    monkeypatch.setenv("CODE_ENRICHMENT_VERSION", "9.9.9")

    entities = [_entity("Alpha")]
    repository = _repository_double(entities)
    container = await _wired_container(repository)

    async def _post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return _llm_response("handler", 0.95)

    with patch.object(httpx.AsyncClient, "post", new=_post):
        handler = HandlerCodeEnrichmentEffect(container=container)
        result = await handler.handle(
            ModelCodeEnrichmentRequest(correlation_id=str(uuid4()), batch_size=5)
        )

    assert result.enriched_count == 1
    assert result.failed_count == 0
    assert result.enrichment_version == "9.9.9"
    repository.get_entities_needing_enrichment.assert_awaited_once_with(limit=5)
    repository.update_enrichment.assert_awaited_once_with(
        entity_id=str(entities[0]["id"]),
        classification="handler",
        llm_description="Handles incoming HTTP requests.",
        architectural_pattern="handler",
        classification_confidence=0.95,
        enrichment_version="9.9.9",
    )


async def test_one_registration_serves_both_nodes() -> None:
    """A single registration resolves for both handlers' DI key.

    Both handler modules now import one class object. The container also keys
    registrations on ``interface.__name__``, so this asserts the two facts that
    together kill the "register against the wrong module's object" footgun.
    """
    from omnimarket.nodes.node_code_embedding_effect.handlers import (
        handler_code_embedding_effect as embedding_mod,
    )
    from omnimarket.nodes.node_code_enrichment_effect.handlers import (
        handler_code_enrichment_effect as enrichment_mod,
    )

    assert (
        embedding_mod.ProtocolCodeEntityRepository
        is enrichment_mod.ProtocolCodeEntityRepository
        is ProtocolCodeEntityRepository
    )

    repository = _repository_double([])
    container = await _wired_container(repository)

    resolved_embedding = container.get_service(
        embedding_mod.ProtocolCodeEntityRepository  # type: ignore[type-abstract]
    )
    resolved_enrichment = container.get_service(
        enrichment_mod.ProtocolCodeEntityRepository  # type: ignore[type-abstract]
    )
    assert resolved_embedding is repository
    assert resolved_enrichment is repository


async def test_production_adapter_satisfies_the_protocol() -> None:
    """The shipped adapter structurally implements the canonical protocol.

    ``runtime_checkable`` only checks method presence, so this is a shape
    assertion, not a signature one — the signature contract is enforced by mypy
    --strict on the adapter module and by the behaviour tests.
    """
    adapter = RepositoryCodeEntityPostgres(dsn="postgresql://unused/db")
    assert isinstance(adapter, ProtocolCodeEntityRepository)


async def test_plugin_registers_the_real_postgres_adapter_by_default() -> None:
    """Unpatched, the plugin puts the production adapter in the container.

    Guards against the provider silently degrading to a null object or a stub:
    the thing registered must be the Postgres implementation.
    """
    container = ModelONEXContainer()
    plugin = PluginCodeEntityRepository()
    await plugin.initialize(_plugin_config(container))
    result = await plugin.wire_handlers(_plugin_config(container))

    assert result.success, result.error_message
    resolved = container.get_service(
        ProtocolCodeEntityRepository  # type: ignore[type-abstract]
    )
    assert isinstance(resolved, RepositoryCodeEntityPostgres)

    await plugin.shutdown(_plugin_config(container))
