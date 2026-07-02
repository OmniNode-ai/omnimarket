# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full outcome coverage for node_antipattern_match_effect at the mock-injected I/O
boundary, driven over the canonical in-memory bus.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype effect).

The match effect embeds a query and searches a Qdrant collection for nearest
antipattern neighbours. Its I/O seams are (a) the Qdrant client and (b) the
embedding HTTP endpoint. The Qdrant client is mocked by CONSTRUCTOR INJECTION
(``_MockQdrant`` handed to ``_MatchEffectBusHarness``) -- never monkeypatched. The
embedding endpoint has no injectable client seam (the handler builds
``httpx.AsyncClient`` internally), so the embedding boundary is stubbed with the
same ``httpx.AsyncClient`` patch the shipped unit suite uses -- httpx is neither
subprocess nor asyncpg, and no real network or Qdrant is touched.

The real effect handler runs unchanged behind a thin bus harness whose
positional-model ``handle`` lets ``LocalRuntimeBusAdapter`` drive it over the
in-memory ``integration_event_bus``. The terminal ``ModelAntipatternMatchResponse``
is read back off the declared response topic and typed result fields are asserted.

Outcomes covered: success (matches above threshold), threshold filtering, max_results
cap, freshness re-rank, Qdrant-unavailable graceful skip, embedding-failure graceful
skip, missing-endpoint fail (no output published), empty-query fail (no output),
and determinism across repeated publishes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect import (
    HandlerAntipatternMatchEffect,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_request import (
    ModelAntipatternMatchRequest,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_response import (
    ModelAntipatternMatchResponse,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_HTTP_TARGET = (
    "omnimarket.nodes.node_antipattern_match_effect.handlers."
    "handler_antipattern_match_effect.httpx.AsyncClient"
)

# Declared wire strings (contract.yaml -> event_bus). Pinned in the state-coverage
# module.
TOPIC_MATCH_REQUESTED = "onex.cmd.omnimarket.antipattern-match-requested.v1"
TOPIC_MATCH_RESPONSE = "onex.evt.omnimarket.antipattern-match-response.v1"

_ENDPOINT = "http://embed.test:8100"


class _MatchEffectBusHarness:
    """Bus-drivable wrapper: a positional-model ``handle`` that forwards to the real
    effect handler with the Qdrant client + embedding endpoint injected."""

    def __init__(
        self,
        *,
        qdrant_client: Any,
        embedding_endpoint: str | None = _ENDPOINT,
    ) -> None:
        self._real = HandlerAntipatternMatchEffect()
        self._qdrant = qdrant_client
        self._endpoint = embedding_endpoint

    async def handle(
        self, request: ModelAntipatternMatchRequest
    ) -> ModelAntipatternMatchResponse:
        return await self._real.handle(
            correlation_id=request.correlation_id,
            code_text=request.code_text,
            description=request.description,
            min_similarity=request.min_similarity,
            max_results=request.max_results,
            freshness_decay_factor=request.freshness_decay_factor,
            qdrant_client=self._qdrant,
            embedding_endpoint_override=(
                request.embedding_endpoint_override or self._endpoint
            ),
            qdrant_collection_override=request.qdrant_collection_override,
        )


def _make_qdrant(results: list[Any] | None = None) -> MagicMock:
    qdrant = MagicMock()
    # MagicMock(name=...) sets the repr name, not a .name attribute; expose a real
    # .name so any collection-name comparison sees the literal string.
    collection_stub = MagicMock()
    collection_stub.name = "onex_antipatterns"
    qdrant.get_collections.return_value = MagicMock(collections=[collection_stub])
    qdrant.search.return_value = results or []
    return qdrant


def _hit(
    *,
    score: float = 0.9,
    name: str = "test_antipattern",
    discovered_at: str | None = None,
) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {
        "name": name,
        "severity": "ERROR",
        "enforcement": "blocking",
        "category": "architecture",
        "description": "Test description.",
        "rationale": "Test rationale.",
        "source_ticket": "OMN-9999",
        "registry_version": "1.0.0",
    }
    if discovered_at is not None:
        hit.payload["discovered_at"] = discovered_at
    return hit


def _mock_http(embedding: list[float]) -> AsyncMock:
    mock_http = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": embedding}]}
    mock_http.post = AsyncMock(return_value=response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    return mock_http


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _request(**overrides: Any) -> ModelAntipatternMatchRequest:
    params: dict[str, Any] = {
        "correlation_id": "match-corr-001",
        "code_text": "def god_function(): return 1",
        "min_similarity": 0.75,
        "max_results": 5,
    }
    params.update(overrides)
    return ModelAntipatternMatchRequest(**params)


async def _drive(
    bus: Any,
    harness: _MatchEffectBusHarness,
    request: ModelAntipatternMatchRequest,
) -> ModelAntipatternMatchResponse | None:
    """Publish a match request over the bus; return the terminal response, or None
    when the handler raised (adapter suppresses output on error)."""
    adapter = LocalRuntimeBusAdapter(
        handler=harness,
        handler_name="antipattern-match-effect",
        input_model_cls=ModelAntipatternMatchRequest,
        output_topic=TOPIC_MATCH_RESPONSE,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_MATCH_REQUESTED,
        on_message=adapter.on_message,
        group_id="omnimarket-antipattern-match-effect-test",
    )
    await bus.publish(
        TOPIC_MATCH_REQUESTED,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    published = await bus.get_event_history(topic=TOPIC_MATCH_RESPONSE)
    if not published:
        return None
    assert len(published) == 1
    return ModelAntipatternMatchResponse.model_validate(json.loads(published[-1].value))


@pytest.mark.integration
async def test_success_returns_matches_above_threshold_over_bus(
    integration_event_bus: Any,
) -> None:
    """Success outcome: candidates above min_similarity are returned; below are
    filtered."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant(
            [
                _hit(score=0.92, name="antipattern_a"),
                _hit(score=0.80, name="antipattern_b"),
                _hit(score=0.60, name="antipattern_c"),  # below threshold
            ]
        )
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(bus, harness, _request())
        assert result is not None
        assert result.correlation_id == "match-corr-001"
        names = {m.antipattern_name for m in result.matches}
        assert names == {"antipattern_a", "antipattern_b"}
        assert result.total_candidates_searched == 3
        assert result.error_message is None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_matches_ordered_by_adjusted_score_over_bus(
    integration_event_bus: Any,
) -> None:
    """Matches are returned sorted by adjusted_score descending."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant(
            [_hit(score=0.80, name="lower"), _hit(score=0.92, name="higher")]
        )
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(bus, harness, _request())
        assert result is not None
        assert result.matches[0].antipattern_name == "higher"
        assert result.matches[1].antipattern_name == "lower"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_max_results_caps_output_over_bus(
    integration_event_bus: Any,
) -> None:
    """max_results bounds the number of returned matches."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant([_hit(score=0.90, name=f"ap_{i}") for i in range(10)])
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(
                bus, harness, _request(min_similarity=0.0, max_results=3)
            )
        assert result is not None
        assert len(result.matches) == 3
    finally:
        await bus.close()


@pytest.mark.integration
async def test_freshness_reranks_recent_antipattern_over_bus(
    integration_event_bus: Any,
) -> None:
    """A recent, slightly-lower-scored antipattern outranks an old higher one when
    the freshness boost is applied."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant(
            [
                _hit(score=0.90, name="old_ap", discovered_at=_iso_days_ago(3650)),
                _hit(score=0.85, name="recent_ap", discovered_at=_iso_days_ago(1)),
            ]
        )
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(bus, harness, _request(freshness_decay_factor=0.20))
        assert result is not None
        assert result.matches[0].antipattern_name == "recent_ap"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_qdrant_unavailable_graceful_skip_over_bus(
    integration_event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qdrant-unavailable failure mode: no client + no QDRANT_HOST -> empty matches
    with an error_message (no embedding call is even attempted)."""
    bus = integration_event_bus
    await bus.start()
    try:
        monkeypatch.delenv("QDRANT_HOST", raising=False)
        harness = _MatchEffectBusHarness(qdrant_client=None)
        result = await _drive(bus, harness, _request())
        assert result is not None
        assert result.matches == ()
        assert result.error_message is not None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_embedding_failure_graceful_skip_over_bus(
    integration_event_bus: Any,
) -> None:
    """Embedding-failure mode: the embedding POST raises -> empty matches with an
    error_message; the Qdrant search is never reached."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant([_hit(score=0.9)])
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            failing = AsyncMock()
            failing.post = AsyncMock(side_effect=Exception("connection refused"))
            failing.__aenter__ = AsyncMock(return_value=failing)
            failing.__aexit__ = AsyncMock(return_value=None)
            http_cls.return_value = failing
            result = await _drive(bus, harness, _request())
        assert result is not None
        assert result.matches == ()
        assert result.error_message is not None
        qdrant.search.assert_not_called()
    finally:
        await bus.close()


@pytest.mark.integration
async def test_qdrant_search_failure_graceful_skip_over_bus(
    integration_event_bus: Any,
) -> None:
    """Qdrant search raising is caught -> empty matches with an error_message."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant()
        qdrant.search.side_effect = RuntimeError("qdrant boom")
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            result = await _drive(bus, harness, _request())
        assert result is not None
        assert result.matches == ()
        assert result.error_message is not None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_missing_embedding_endpoint_publishes_no_output_over_bus(
    integration_event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-blocked failure: with no endpoint override and EMBEDDING_MODEL_URL unset,
    the handler raises OSError -> the adapter suppresses output (nothing on the
    response topic)."""
    bus = integration_event_bus
    await bus.start()
    try:
        monkeypatch.delenv("EMBEDDING_MODEL_URL", raising=False)
        harness = _MatchEffectBusHarness(
            qdrant_client=_make_qdrant(), embedding_endpoint=None
        )
        result = await _drive(bus, harness, _request())
        assert result is None, "handler raise must not publish a response"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_empty_query_publishes_no_output_over_bus(
    integration_event_bus: Any,
) -> None:
    """A request with neither code_text nor description raises ValueError -> no
    response is published."""
    bus = integration_event_bus
    await bus.start()
    try:
        harness = _MatchEffectBusHarness(qdrant_client=_make_qdrant())
        request = ModelAntipatternMatchRequest(
            correlation_id="match-empty-001",
            code_text=None,
            description=None,
        )
        result = await _drive(bus, harness, request)
        assert result is None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_repeated_publishes_are_deterministic_over_bus(
    integration_event_bus: Any,
) -> None:
    """Two identical publishes yield identical response payloads (deterministic given
    a fixed embedding + Qdrant result set)."""
    bus = integration_event_bus
    await bus.start()
    try:
        qdrant = _make_qdrant([_hit(score=0.9, name="stable_ap")])
        harness = _MatchEffectBusHarness(qdrant_client=qdrant)
        adapter = LocalRuntimeBusAdapter(
            handler=harness,
            handler_name="antipattern-match-effect",
            input_model_cls=ModelAntipatternMatchRequest,
            output_topic=TOPIC_MATCH_RESPONSE,
            bus=bus,
        )
        await bus.subscribe(
            TOPIC_MATCH_REQUESTED,
            on_message=adapter.on_message,
            group_id="omnimarket-antipattern-match-effect-test",
        )
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http([0.1] * 8)
            for _ in range(2):
                await bus.publish(
                    TOPIC_MATCH_REQUESTED,
                    key=None,
                    value=_request().model_dump_json().encode("utf-8"),
                )
        published = await bus.get_event_history(topic=TOPIC_MATCH_RESPONSE)
        assert len(published) == 2
        first = ModelAntipatternMatchResponse.model_validate(
            json.loads(published[0].value)
        )
        second = ModelAntipatternMatchResponse.model_validate(
            json.loads(published[1].value)
        )
        assert first.matches == second.matches
    finally:
        await bus.close()
