# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: one antipattern-validate command reaches one antipattern-validated event.

The three antipattern nodes are wired from their own ``contract.yaml`` files over
the in-memory bus, the way the runtime wires them on the Kafka path: every topic
and every output class -> topic mapping is read from the contract, none is typed
here. The chain is

    validate (orchestrator) -> match-requested (match effect) -> match-response
    (orchestrator) -> classify (classifier compute) -> classified (orchestrator)
    -> antipattern-validated (terminal)

The embedding endpoint and the Qdrant client are the only stubs: the Qdrant client
is handed to the real effect handler (constructor injection), and the embedding
HTTP call is patched at ``httpx.AsyncClient`` like the effect's own golden chain.

Outcomes: a blocking match, no match, a short file the classifier exempts, the
Qdrant-unavailable graceful skip, the missing-embedding-endpoint graceful skip,
and two commands in flight at once (each gets exactly one terminal event).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import yaml

import omnimarket.nodes as nodes_pkg
from omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect import (
    HandlerAntipatternMatchEffect,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_request import (
    ModelAntipatternMatchRequest,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_response import (
    ModelAntipatternMatchResponse,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.handlers.handler_antipattern_classifier import (
    HandlerAntipatternClassifier,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_NODES_DIR = Path(nodes_pkg.__file__).parent
_ORCHESTRATOR = "node_semantic_antipattern_validator_orchestrator"
_MATCH = "node_antipattern_match_effect"
_CLASSIFIER = "node_semantic_antipattern_classifier_compute"
_HTTP_TARGET = (
    "omnimarket.nodes.node_antipattern_match_effect.handlers."
    "handler_antipattern_match_effect.httpx.AsyncClient"
)
_ENDPOINT = "http://embed.test:8100"
_LONG_FILE = "\n".join(f"def fn_{i}(): return {i}" for i in range(14)) + "\n"
_SHORT_FILE = "def only(): return 1\n"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_NODES_DIR / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _published_events(contract: dict[str, Any]) -> dict[str, str]:
    """event_type -> topic, the way the runtime's published_events reader keys it."""
    return {
        entry["event_type"].removeprefix("Model"): entry["topic"]
        for entry in contract.get("published_events", [])
    }


def _symbol(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


class _MatchEffectHarness:
    """Positional-model ``handle`` over the real effect handler."""

    def __init__(self, *, qdrant_client: Any, endpoint: str | None) -> None:
        self._real = HandlerAntipatternMatchEffect(qdrant_client=qdrant_client)
        self._endpoint = endpoint

    async def handle(
        self, request: ModelAntipatternMatchRequest
    ) -> ModelAntipatternMatchResponse:
        if request.embedding_endpoint_override is None and self._endpoint is not None:
            request = request.model_copy(
                update={"embedding_endpoint_override": self._endpoint}
            )
        return await self._real.handle(request)


def _hit(
    *, score: float, name: str = "god_object", enforcement: str = "blocking"
) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {
        "name": name,
        "severity": "ERROR",
        "enforcement": enforcement,
        "category": "architecture",
        "description": "Class does too many unrelated things.",
        "rationale": "Hard to test.",
    }
    return hit


def _qdrant(hits: list[MagicMock]) -> MagicMock:
    qdrant = MagicMock()
    qdrant.search.return_value = hits
    return qdrant


def _mock_http() -> AsyncMock:
    mock_http = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": [0.1] * 8}]}
    mock_http.post = AsyncMock(return_value=response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    return mock_http


class _RecordingBus:
    """Delegates to the bus and notes each topic as it is published.

    The in-memory bus delivers inside ``publish``, so a hop's answer is published
    before the publish that triggered it returns; delivery order would read
    backwards. Publication order is the causal order the test asserts on.
    """

    def __init__(self, inner: Any, order: list[str]) -> None:
        self._inner = inner
        self._order = order

    async def publish(self, topic: str, *args: Any, **kwargs: Any) -> Any:
        self._order.append(topic)
        return await self._inner.publish(topic, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Chain:
    """The three nodes subscribed on one in-memory bus, wired from their contracts."""

    def __init__(
        self, bus: Any, *, qdrant_client: Any, endpoint: str | None = _ENDPOINT
    ) -> None:
        self.order: list[str] = []
        self.bus = _RecordingBus(bus, self.order)
        self.qdrant_client = qdrant_client
        self.endpoint = endpoint
        self.orchestrator = _contract(_ORCHESTRATOR)
        self.match = _contract(_MATCH)
        self.classifier = _contract(_CLASSIFIER)

    async def start(self) -> None:
        await self.bus.start()
        orch_events = _published_events(self.orchestrator)
        orch_default = self.orchestrator["event_bus"]["publish_topics"][0]
        command_topic = self.orchestrator["runtime_dispatch"]["command_topic"]
        for route in self.orchestrator["handler_routing"]["handlers"]:
            topic = route.get("topic") or command_topic
            handler_cls = _symbol(route["handler"]["module"], route["handler"]["name"])
            await self._subscribe(
                topic,
                handler_cls(),
                f"orchestrator-{route['operation']}",
                self._input_model(route),
                orch_default,
                orch_events,
            )
        match_topic = self.match["event_bus"]["subscribe_topics"][0]
        await self._subscribe(
            match_topic,
            _MatchEffectHarness(
                qdrant_client=self.qdrant_client, endpoint=self.endpoint
            ),
            "match-effect",
            ModelAntipatternMatchRequest,
            self.match["event_bus"]["publish_topics"][0],
            {},
        )
        classify_topic = self.classifier["event_bus"]["subscribe_topics"][0]
        await self._subscribe(
            classify_topic,
            HandlerAntipatternClassifier(),
            "classifier",
            ModelAntipatternClassifyRequest,
            self.classifier["event_bus"]["publish_topics"][0],
            {},
        )

    def _input_model(self, route: dict[str, Any]) -> Any:
        model_ref = route.get("event_model")
        if model_ref is not None:
            return _symbol(model_ref["module"], model_ref["name"])
        module, _, name = self.orchestrator["handler"]["input_model"].rpartition(".")
        return _symbol(module, name)

    async def _subscribe(
        self,
        topic: str,
        handler: Any,
        name: str,
        input_model: type,
        output_topic: str,
        published_events: dict[str, str],
    ) -> None:
        adapter = LocalRuntimeBusAdapter(
            handler=handler,
            handler_name=name,
            input_model_cls=input_model,
            output_topic=output_topic,
            bus=self.bus,
            published_events=published_events,
            multi_event_seam_enabled=bool(published_events),
        )
        await self.bus.subscribe(
            topic, on_message=adapter.on_message, group_id=f"chain-{name}"
        )

    async def validate(
        self,
        *,
        content: str,
        correlation_id: str | None = None,
        file_path: str = "services/example_app/settings_loader.py",
        threshold: float = 0.80,
    ) -> str:
        cid = correlation_id or str(uuid4())
        command_topic = self.orchestrator["runtime_dispatch"]["command_topic"]
        payload = {
            "file_path": file_path,
            "file_content": content,
            "enforcement_mode": "blocking",
            "similarity_threshold": threshold,
            "correlation_id": cid,
        }
        await self.bus.publish(
            command_topic, key=None, value=json.dumps(payload).encode("utf-8")
        )
        return cid

    async def events(self, topic: str, correlation_id: str) -> list[dict[str, Any]]:
        history = await self.bus.get_event_history(topic=topic)
        decoded = [json.loads(message.value) for message in history]
        return [e for e in decoded if _correlation_of(e) == correlation_id]

    @property
    def validated_topic(self) -> str:
        return str(self.orchestrator["terminal_event"])

    @property
    def match_requested_topic(self) -> str:
        return str(self.match["event_bus"]["subscribe_topics"][0])

    @property
    def match_response_topic(self) -> str:
        return str(self.match["event_bus"]["publish_topics"][0])


def _correlation_of(event: dict[str, Any]) -> str | None:
    payload = event.get("payload", event)
    value = payload.get("correlation_id") if isinstance(payload, dict) else None
    return str(value) if value is not None else None


def _body(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", event)
    assert isinstance(payload, dict)
    return payload


async def _one_validated(chain: _Chain, cid: str) -> dict[str, Any]:
    validated = await chain.events(chain.validated_topic, cid)
    assert len(validated) == 1, (
        f"expected exactly one antipattern-validated event for {cid}, got "
        f"{len(validated)}; topics seen in order: {chain.order}"
    )
    return _body(validated[0])


@pytest.mark.integration
async def test_blocking_match_reaches_validated_with_one_blocking_violation(
    integration_event_bus: Any,
) -> None:
    chain = _Chain(integration_event_bus, qdrant_client=_qdrant([_hit(score=0.93)]))
    try:
        await chain.start()
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http()
            cid = await chain.validate(content=_LONG_FILE)
        validated = await _one_validated(chain, cid)
        assert validated["correlation_id"] == cid
        assert validated["file_path"] == "services/example_app/settings_loader.py"
        assert validated["has_blocking_violation"] is True
        assert validated["violation_count"] == 1
        assert validated["violations"][0]["label"] == "god_object"
        assert validated["violations"][0]["is_blocking"] is True
        assert validated["match_error"] is None
        # the match request and its response precede the terminal event
        order = chain.order
        assert order.index(chain.match_requested_topic) < order.index(
            chain.match_response_topic
        )
        assert order.index(chain.match_response_topic) < order.index(
            chain.validated_topic
        )
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
async def test_no_match_yields_one_validated_with_zero_violations(
    integration_event_bus: Any,
) -> None:
    chain = _Chain(integration_event_bus, qdrant_client=_qdrant([]))
    try:
        await chain.start()
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http()
            cid = await chain.validate(content=_LONG_FILE)
        validated = await _one_validated(chain, cid)
        assert validated["violation_count"] == 0
        assert validated["has_blocking_violation"] is False
        assert validated["violations"] == []
        assert validated["match_error"] is None
        assert len(await chain.events(chain.match_requested_topic, cid)) == 1
        assert len(await chain.events(chain.match_response_topic, cid)) == 1
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
async def test_short_file_match_is_exempt_and_still_reaches_validated(
    integration_event_bus: Any,
) -> None:
    chain = _Chain(integration_event_bus, qdrant_client=_qdrant([_hit(score=0.97)]))
    try:
        await chain.start()
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http()
            cid = await chain.validate(content=_SHORT_FILE)
        validated = await _one_validated(chain, cid)
        assert validated["violation_count"] == 0
        assert validated["has_blocking_violation"] is False
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
async def test_qdrant_unavailable_graceful_skip_reaches_validated(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QDRANT_HOST", raising=False)
    chain = _Chain(integration_event_bus, qdrant_client=None)
    try:
        await chain.start()
        cid = await chain.validate(content=_LONG_FILE)
        validated = await _one_validated(chain, cid)
        assert validated["violation_count"] == 0
        assert validated["has_blocking_violation"] is False
        assert "Qdrant unavailable" in validated["match_error"]
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
async def test_missing_embedding_endpoint_graceful_skip_reaches_validated(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL_URL", raising=False)
    chain = _Chain(
        integration_event_bus, qdrant_client=_qdrant([_hit(score=0.95)]), endpoint=None
    )
    try:
        await chain.start()
        cid = await chain.validate(content=_LONG_FILE)
        validated = await _one_validated(chain, cid)
        assert validated["violation_count"] == 0
        assert "EMBEDDING_MODEL_URL" in validated["match_error"]
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
async def test_two_commands_in_flight_each_get_one_validated(
    integration_event_bus: Any,
) -> None:
    chain = _Chain(integration_event_bus, qdrant_client=_qdrant([_hit(score=0.91)]))
    try:
        await chain.start()
        with patch(_HTTP_TARGET) as http_cls:
            http_cls.return_value = _mock_http()
            first = await chain.validate(content=_LONG_FILE, file_path="a/first.py")
            second = await chain.validate(content=_SHORT_FILE, file_path="b/second.py")
        one = await _one_validated(chain, first)
        two = await _one_validated(chain, second)
        assert one["file_path"] == "a/first.py"
        assert one["violation_count"] == 1
        assert two["file_path"] == "b/second.py"
        assert two["violation_count"] == 0
    finally:
        await integration_event_bus.close()
