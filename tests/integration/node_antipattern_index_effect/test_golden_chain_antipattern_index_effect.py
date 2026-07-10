# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full outcome coverage for node_antipattern_index_effect at the mock-injected I/O
boundary, driven over the canonical in-memory bus.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype effect).
OMN-14242 (thin canonical handler shape).

The index effect embeds each vector-enabled antipattern registry entry and upserts
it into a Qdrant collection, with idempotency keyed on the registry version. Its I/O
seams are (a) the Qdrant client, (b) the registry loader, and (c) the embedding HTTP
endpoint. The Qdrant client and registry loader are mocked by CONSTRUCTOR INJECTION
directly on ``HandlerAntipatternIndexEffect`` -- never monkeypatched. The embedding
endpoint has no injectable client seam, so it is stubbed with the same
``httpx.AsyncClient`` patch the shipped unit suite uses. No real network or Qdrant is
touched.

The real effect handler is driven directly: DI (Qdrant client + registry loader) is
constructor-injected on ``HandlerAntipatternIndexEffect`` and its positional-model
``handle(payload: ModelAntipatternIndexRequest)`` lets ``LocalRuntimeBusAdapter``
drive it over the in-memory ``integration_event_bus`` with no wrapper shim. The
embedding endpoint override travels on the request model itself. The terminal
``ModelAntipatternIndexResult`` is read back off the declared completed topic and
typed result fields are asserted.

Outcomes covered: success (entries indexed + upserted), no-vector-entries skip,
idempotent no-op (version already indexed), force-reindex bypass, embedding-failure
skip accounting, Qdrant-unavailable graceful skip, missing-endpoint fail (no output
published), and determinism across repeated publishes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect import (
    HandlerAntipatternIndexEffect,
)
from omnimarket.nodes.node_antipattern_index_effect.models.model_antipattern_index_request import (
    ModelAntipatternIndexRequest,
)
from omnimarket.nodes.node_antipattern_index_effect.models.model_antipattern_index_result import (
    ModelAntipatternIndexResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_HTTP_TARGET = (
    "omnimarket.nodes.node_antipattern_index_effect.handlers."
    "handler_antipattern_index_effect.httpx.AsyncClient"
)

# Declared wire strings (contract.yaml -> event_bus). Pinned in the state-coverage
# module.
TOPIC_INDEX_REQUESTED = "onex.cmd.omnimarket.antipattern-index-requested.v1"
TOPIC_INDEX_COMPLETED = "onex.evt.omnimarket.antipattern-index-completed.v1"

_ENDPOINT = "http://embed.test:8100"


# --------------------------------------------------------------------------- #
# Duck-typed registry stand-ins (the real registry loader is injected away).
# --------------------------------------------------------------------------- #
class _FakeExample:
    def __init__(self, kind: str = "bad", label: str = "example", code: str = "pass"):
        self.kind = kind
        self.label = label
        self.code = code


class _FakeEntry:
    def __init__(self, name: str, vector_enabled: bool) -> None:
        self.name = name
        self.vector_enabled = vector_enabled
        self.description = f"Description for {name}."
        self.rationale = f"Rationale for {name}."
        self.examples = [_FakeExample(label=f"Bad {name}", code="x = 1")]
        self.severity = "ERROR"
        self.enforcement = "blocking"
        self.category = "architecture"
        self.pattern_type = "semantic"
        self.source_ticket = "OMN-11913"


class _FakeRegistry:
    def __init__(self, entries: list[Any], version: str = "1.0.0") -> None:
        self.version = version
        self.last_updated = datetime(2026, 5, 24, tzinfo=UTC)
        self.entries = entries


def _registry(vector_entries: int = 2, non_vector_entries: int = 1) -> _FakeRegistry:
    entries: list[Any] = [
        _FakeEntry(f"semantic_{i}", vector_enabled=True) for i in range(vector_entries)
    ]
    entries += [
        _FakeEntry(f"regex_{i}", vector_enabled=False)
        for i in range(non_vector_entries)
    ]
    return _FakeRegistry(entries=entries)


def _make_handler(
    *, qdrant_client: Any, registry: _FakeRegistry
) -> HandlerAntipatternIndexEffect:
    """Construct the real handler with DI (Qdrant client + registry loader).

    No wrapper shim: the handler is thin/canonical and takes a single typed
    ``ModelAntipatternIndexRequest`` positionally in ``handle()``, so
    ``LocalRuntimeBusAdapter`` can drive it unmodified over the bus.
    """
    return HandlerAntipatternIndexEffect(
        qdrant_client=qdrant_client,
        registry_loader=lambda _root: registry,
    )


def _make_qdrant(already_indexed_version: str | None = None) -> MagicMock:
    qdrant = MagicMock()
    # NOTE: MagicMock(name=...) sets the mock's repr name, NOT a .name attribute --
    # _ensure_collection compares c.name against the collection string, so the stub
    # must expose a real .name for the existing-collection path to be exercised.
    collection_stub = MagicMock()
    collection_stub.name = "onex_antipatterns"
    qdrant.get_collections.return_value = MagicMock(collections=[collection_stub])
    if already_indexed_version is not None:
        qdrant.retrieve.return_value = [
            MagicMock(
                payload={"registry_version": already_indexed_version, "_meta": True}
            )
        ]
    else:
        qdrant.retrieve.return_value = []
    qdrant.upsert = MagicMock()
    return qdrant


def _mock_http(embedding: list[float]) -> AsyncMock:
    mock_http = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": embedding}]}
    mock_http.post = AsyncMock(return_value=response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    return mock_http


def _request(**overrides: Any) -> ModelAntipatternIndexRequest:
    params: dict[str, Any] = {
        "correlation_id": "index-corr-001",
        "embedding_endpoint_override": _ENDPOINT,
    }
    params.update(overrides)
    return ModelAntipatternIndexRequest(**params)


async def _drive(
    bus: Any,
    handler: HandlerAntipatternIndexEffect,
    request: ModelAntipatternIndexRequest,
) -> ModelAntipatternIndexResult | None:
    """Publish an index request over the bus; return the terminal result, or None
    when the handler raised (adapter suppresses output on error)."""
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name="antipattern-index-effect",
        input_model_cls=ModelAntipatternIndexRequest,
        output_topic=TOPIC_INDEX_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_INDEX_REQUESTED,
        on_message=adapter.on_message,
        group_id="omnimarket-antipattern-index-effect-test",
    )
    await bus.publish(
        TOPIC_INDEX_REQUESTED,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    published = await bus.get_event_history(topic=TOPIC_INDEX_COMPLETED)
    if not published:
        return None
    assert len(published) == 1
    return ModelAntipatternIndexResult.model_validate(json.loads(published[-1].value))


@pytest.mark.integration
async def test_success_indexes_vector_entries_over_bus(
    integration_event_bus: Any,
) -> None:
    """Success outcome: every vector-enabled entry is embedded + upserted."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _make_handler(
            qdrant_client=_make_qdrant(),
            registry=_registry(vector_entries=2, non_vector_entries=1),
        )
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(bus, handler, _request())
        assert result is not None
        assert result.correlation_id == "index-corr-001"
        assert result.indexed_count == 2
        assert len(result.vector_ids) == 2
        assert result.registry_version == "1.0.0"
        assert result.was_no_op is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_no_vector_entries_zero_indexed_over_bus(
    integration_event_bus: Any,
) -> None:
    """No vector-enabled entries -> indexed 0, everything skipped, no upsert."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant()
        handler = _make_handler(
            qdrant_client=qdrant,
            registry=_registry(vector_entries=0, non_vector_entries=3),
        )
        result = await _drive(bus, handler, _request())
        assert result is not None
        assert result.indexed_count == 0
        assert result.skipped_count == 3
        qdrant.upsert.assert_not_called()
    finally:
        await bus.close()


@pytest.mark.integration
async def test_idempotent_no_op_when_version_already_indexed_over_bus(
    integration_event_bus: Any,
) -> None:
    """Idempotency: registry version already indexed -> no-op, no upsert."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant(already_indexed_version="1.0.0")
        handler = _make_handler(
            qdrant_client=qdrant, registry=_registry(vector_entries=2)
        )
        result = await _drive(bus, handler, _request())
        assert result is not None
        assert result.was_no_op is True
        assert result.indexed_count == 0
        qdrant.upsert.assert_not_called()
    finally:
        await bus.close()


@pytest.mark.integration
async def test_force_reindex_bypasses_idempotency_over_bus(
    integration_event_bus: Any,
) -> None:
    """force_reindex re-indexes even when the version is already present."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant(already_indexed_version="1.0.0")
        handler = _make_handler(
            qdrant_client=qdrant, registry=_registry(vector_entries=1)
        )
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.3] * 8)
            result = await _drive(bus, handler, _request(force_reindex=True))
        assert result is not None
        assert result.was_no_op is False
        assert result.indexed_count == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_embedding_failure_increments_skipped_over_bus(
    integration_event_bus: Any,
) -> None:
    """Embedding-failure mode: each entry whose embedding fails increments skipped."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _make_handler(
            qdrant_client=_make_qdrant(),
            registry=_registry(vector_entries=2, non_vector_entries=0),
        )
        with patch(_HTTP_TARGET) as http_cls:
            failing = AsyncMock()
            failing.post = AsyncMock(side_effect=Exception("connection refused"))
            failing.__aenter__ = AsyncMock(return_value=failing)
            failing.__aexit__ = AsyncMock(return_value=None)
            http_cls.return_value = failing
            result = await _drive(bus, handler, _request())
        assert result is not None
        assert result.indexed_count == 0
        assert result.skipped_count == 2
    finally:
        await bus.close()


@pytest.mark.integration
async def test_qdrant_unavailable_graceful_skip_over_bus(
    integration_event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qdrant-unavailable failure mode: no client + no QDRANT_HOST -> zero counts,
    non-error terminal result."""
    bus = integration_event_bus
    await bus.start()
    try:
        monkeypatch.delenv("QDRANT_HOST", raising=False)
        handler = _make_handler(
            qdrant_client=None, registry=_registry(vector_entries=2)
        )
        result = await _drive(bus, handler, _request())
        assert result is not None
        assert result.indexed_count == 0
        assert result.skipped_count == 0
        assert result.was_no_op is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_missing_embedding_endpoint_publishes_no_output_over_bus(
    integration_event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-blocked failure: no endpoint override and EMBEDDING_MODEL_URL unset ->
    the handler raises OSError -> nothing is published on the completed topic."""
    bus = integration_event_bus
    await bus.start()
    try:
        monkeypatch.delenv("EMBEDDING_MODEL_URL", raising=False)
        handler = _make_handler(
            qdrant_client=_make_qdrant(),
            registry=_registry(vector_entries=1),
        )
        result = await _drive(bus, handler, _request(embedding_endpoint_override=None))
        assert result is None, "handler raise must not publish a completion event"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_repeated_publishes_are_deterministic_over_bus(
    integration_event_bus: Any,
) -> None:
    """Two identical publishes over fresh Qdrant mocks yield identical index counts."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _make_handler(
            qdrant_client=_make_qdrant(),
            registry=_registry(vector_entries=2, non_vector_entries=1),
        )
        adapter = LocalRuntimeBusAdapter(
            handler=handler,
            handler_name="antipattern-index-effect",
            input_model_cls=ModelAntipatternIndexRequest,
            output_topic=TOPIC_INDEX_COMPLETED,
            bus=bus,
        )
        await bus.subscribe(
            TOPIC_INDEX_REQUESTED,
            on_message=adapter.on_message,
            group_id="omnimarket-antipattern-index-effect-test",
        )
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            for _ in range(2):
                await bus.publish(
                    TOPIC_INDEX_REQUESTED,
                    key=None,
                    value=_request().model_dump_json().encode("utf-8"),
                )
        published = await bus.get_event_history(topic=TOPIC_INDEX_COMPLETED)
        assert len(published) == 2
        first = ModelAntipatternIndexResult.model_validate(
            json.loads(published[0].value)
        )
        second = ModelAntipatternIndexResult.model_validate(
            json.loads(published[1].value)
        )
        assert first.indexed_count == second.indexed_count == 2
    finally:
        await bus.close()
