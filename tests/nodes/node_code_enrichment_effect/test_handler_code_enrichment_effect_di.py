# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Boot-resolvability + behavior-preservation proof for HandlerCodeEnrichmentEffect.

OMN-15229 (slice B2 of OMN-15027). The handler previously took a required,
default-less ``repository`` param, which is not in the boot resolver's
``_KNOWN_INJECTABLE`` set — so once ``node_code_enrichment_effect/contract.yaml``
is converted to the nested ``handler.{name,module}`` shape (sequential slice B3),
``tests/test_handler_routing_boot_resolvable.py`` would report::

    node_code_enrichment_effect :: op=enrich_batch ::
      ...handler_code_enrichment_effect.HandlerCodeEnrichmentEffect ::
      required-unresolvable=['repository']

The contract is still flat today, so the real gate does not yet *see* this class
and running it unchanged proves nothing. This module therefore drives the real
gate helper ``_required_non_injectable_params`` directly against the real class —
no surrogate — so the constraint is enforced independently of contract shape.

This file lives under ``tests/`` on purpose: the node-local module at
``src/omnimarket/nodes/node_code_enrichment_effect/tests/`` is outside
``testpaths = ["tests"]`` and is not collected by CI.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from omnibase_core.container import ModelONEXContainer
from omnibase_core.errors.error_service_resolution import ServiceResolutionError

from omnimarket.nodes.node_code_enrichment_effect.handlers.handler_code_enrichment_effect import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    HandlerCodeEnrichmentEffect,
    ProtocolCodeEntityRepository,
)
from omnimarket.nodes.node_code_enrichment_effect.models.model_code_enrichment_request import (
    ModelCodeEnrichmentRequest,
)
from omnimarket.nodes.node_code_enrichment_effect.models.model_code_enrichment_result import (
    ModelCodeEnrichmentResult,
)

# The real boot-resolvability gate helper — imported, not re-implemented, so a
# change to the gate's own definition of "resolvable" propagates here.
from tests.test_handler_routing_boot_resolvable import (
    _KNOWN_INJECTABLE,
    _required_non_injectable_params,
)


def _make_entity(entity_name: str = "MyHandler") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "entity_name": entity_name,
        "entity_type": "class",
        "qualified_name": f"mymodule.{entity_name}",
        "source_repo": "omniintelligence",
        "source_path": "src/mymodule.py",
        "docstring": "Handles incoming requests.",
        "signature": None,
        "bases": ["BaseHandler"],
        "methods": [{"name": "handle"}],
        "fields": None,
        "decorators": None,
    }


def _make_repository_double(entities: list[dict[str, Any]]) -> MagicMock:
    repo = MagicMock()
    repo.get_entities_needing_enrichment = AsyncMock(return_value=entities)
    repo.update_enrichment = AsyncMock()
    return repo


async def _container_with_repository(repo: MagicMock) -> ModelONEXContainer:
    """Register *repo* as the ProtocolCodeEntityRepository provider on a real container."""
    container = ModelONEXContainer()
    await container.service_registry.register_instance(
        ProtocolCodeEntityRepository,  # type: ignore[type-abstract]  # Protocol used as DI key
        repo,
    )
    return container


def _llm_json_response(
    *,
    classification: str,
    confidence: float,
    description: str = "Handles incoming HTTP requests.",
    pattern: str = "handler",
) -> httpx.Response:
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "classification": classification,
                            "confidence": confidence,
                            "description": description,
                            "pattern": pattern,
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


# =============================================================================
# 1. Boot resolvability — drives the real gate helper, no surrogate class
# =============================================================================


@pytest.mark.unit
def test_handler_ctor_has_no_non_injectable_required_params() -> None:
    """The real gate helper reports zero unresolvable ctor params (OMN-15229).

    RED before the refactor: ``['repository']``. GREEN after: ``[]``.
    """
    assert _required_non_injectable_params(HandlerCodeEnrichmentEffect) == []


@pytest.mark.unit
def test_handler_ctor_takes_the_injectable_container() -> None:
    """The single ctor dependency is the boot-injectable ``container``."""
    import inspect

    params = [
        name
        for name in inspect.signature(HandlerCodeEnrichmentEffect).parameters
        if name != "self"
    ]
    assert params == ["container"]
    assert "container" in _KNOWN_INJECTABLE


@pytest.mark.unit
def test_handle_keeps_definition_b_signature() -> None:
    """def-B is preserved: handle(payload: ModelCodeEnrichmentRequest) -> Result.

    Guards against copying HandlerTicketQuery's two-param
    ``handle(correlation_id, input_data)`` shape, which would trip the
    OMN-14355 canon-shape ratchet.
    """
    import inspect

    sig = inspect.signature(HandlerCodeEnrichmentEffect.handle)
    params = [name for name in sig.parameters if name != "self"]
    assert params == ["payload"]
    assert sig.parameters["payload"].annotation in (
        ModelCodeEnrichmentRequest,
        "ModelCodeEnrichmentRequest",
    )
    assert sig.return_annotation in (
        ModelCodeEnrichmentResult,
        "ModelCodeEnrichmentResult",
    )


# =============================================================================
# 2. Behavior preservation through a container-registered double
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrichment_path_preserved_via_container_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM classification reaches the container-resolved repository unchanged."""
    entity = _make_entity("HandlerA")
    repo = _make_repository_double([entity])

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return _llm_json_response(classification="handler", confidence=0.85)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    handler = HandlerCodeEnrichmentEffect(
        container=await _container_with_repository(repo)
    )
    result = await handler.handle(
        ModelCodeEnrichmentRequest(
            correlation_id="omn-15229-di-001",
            llm_endpoint_override="http://test:8001",
            batch_size=10,
        )
    )

    assert result.enriched_count == 1
    assert result.failed_count == 0
    repo.get_entities_needing_enrichment.assert_awaited_once_with(limit=10)
    call_kwargs = repo.update_enrichment.call_args.kwargs
    assert call_kwargs["entity_id"] == str(entity["id"])
    assert call_kwargs["classification"] == "handler"
    assert call_kwargs["classification_confidence"] == 0.85


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_confidence_still_downgrades_to_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant S5 survives the DI change: sub-threshold confidence -> 'other'."""
    repo = _make_repository_double([_make_entity("AmbiguousThing")])
    low_confidence = DEFAULT_CONFIDENCE_THRESHOLD - 0.1

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return _llm_json_response(classification="adapter", confidence=low_confidence)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    handler = HandlerCodeEnrichmentEffect(
        container=await _container_with_repository(repo)
    )
    result = await handler.handle(
        ModelCodeEnrichmentRequest(
            correlation_id="omn-15229-di-002",
            llm_endpoint_override="http://test:8001",
        )
    )

    assert result.enriched_count == 1
    assert repo.update_enrichment.call_args.kwargs["classification"] == "other"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repository_is_not_resolved_at_construction() -> None:
    """Construction must not touch the container — that is what quarantines at boot.

    A handler built against a container with no provider registered constructs
    fine; only ``handle()`` surfaces the wiring gap.
    """
    container = ModelONEXContainer()
    handler = HandlerCodeEnrichmentEffect(container=container)
    assert isinstance(handler, HandlerCodeEnrichmentEffect)


# =============================================================================
# 3. Missing provider fails loud — no silent fallback, no empty result
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_repository_provider_raises_loudly() -> None:
    """No registered ProtocolCodeEntityRepository -> raise, never a zero-count result."""
    handler = HandlerCodeEnrichmentEffect(container=ModelONEXContainer())

    with pytest.raises(ServiceResolutionError, match="ProtocolCodeEntityRepository"):
        await handler.handle(
            ModelCodeEnrichmentRequest(
                correlation_id="omn-15229-di-003",
                llm_endpoint_override="http://test:8001",
            )
        )
