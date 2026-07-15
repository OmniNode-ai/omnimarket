"""OMN-14616: AdapterPatternBBrokerPublish / AdapterPatternBBrokerTerminalConsumer
must expose a def-B dispatch entrypoint (``handle()`` or ``handle_async()``).

Background
----------
Both adapters previously exposed only their domain methods
(``publish()`` / ``wait_for_terminal_event()``) with no ``handle``/
``handle_async``. Auto-wiring's ``_make_dispatch_callback``
(``omnibase_infra.runtime.auto_wiring.handler_wiring``) binds the private
``_missing_handle`` sentinel for any handler exposing neither method, which
raises ``ModelOnexError`` on every real dispatch (OMN-14135/OMN-14510 defect
class, found outside the omnibase_infra-scoped burn-down ratchet).

RED (pre-fix, reproduced by ``test_real_engine_dispatch_binds_missing_handle_pre_fix``
against a bare stand-in mirroring the exact pre-fix class shape): neither
class exposes ``handle``/``handle_async``, so the real, unmodified
``_make_dispatch_callback`` from omnibase_infra binds ``_missing_handle`` and
the produced callback raises ``ModelOnexError`` mentioning
``does not expose a callable handle() or handle_async()`` for ANY dispatch.

GREEN (post-fix): both real adapter classes now expose a callable ``handle``
entrypoint that delegates to their existing domain methods. Driving the exact
same real ``_make_dispatch_callback`` machinery — using the contract's own
declared ``event_model`` (resolved via the real discovery path, i.e. the
production wiring flow, not a hand-typed stand-in) — against a real envelope
completes without raising ``_missing_handle``'s ``ModelOnexError`` and returns
the adapter's real typed response.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_publish import (
    AdapterPatternBBrokerPublish,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_terminal_consumer import (
    AdapterPatternBBrokerTerminalConsumer,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerOriginator,
    EnumPatternBBrokerRecipient,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerPublishReceipt,
    ModelPatternBBrokerTerminalEvent,
    ModelPatternBBrokerWaitPolicy,
)

_BROKER_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "contract.yaml"
)

_MISSING_HANDLE_MESSAGE = "does not expose a callable handle() or handle_async()"


def _make_dispatch_request(
    *, wait_timeout_seconds: int = 300
) -> ModelPatternBBrokerDispatchRequest:
    return ModelPatternBBrokerDispatchRequest(
        correlation_id=uuid4(),
        originator=EnumPatternBBrokerOriginator.omnimarket,
        recipient=EnumPatternBBrokerRecipient.omniclaude,
        skill_name="session-orchestrator",
        wait_policy=ModelPatternBBrokerWaitPolicy(timeout_seconds=wait_timeout_seconds),
    )


class _PreFixAdapterPatternBBrokerPublishStandIn:
    """Mirrors the EXACT pre-fix class shape (verified against dev HEAD at
    the time this ticket was filed: a plain class exposing only
    ``publish()``, no ``handle``/``handle_async``, no base class).

    This is the RED fixture, not a mock of the dispatch machinery — the real,
    unmodified ``_make_dispatch_callback`` from omnibase_infra is what runs
    against it below.
    """

    def __init__(self, *, event_bus: object) -> None:
        self._event_bus = event_bus

    async def publish(
        self, request: ModelPatternBBrokerDispatchRequest
    ) -> ModelPatternBBrokerPublishReceipt:  # pragma: no cover - never reached
        raise AssertionError("publish() must not be reached via _missing_handle")


class _PreFixAdapterPatternBBrokerTerminalConsumerStandIn:
    """Mirrors the EXACT pre-fix class shape for the terminal consumer adapter."""

    def __init__(self, *, event_bus: object) -> None:
        self._event_bus = event_bus

    async def wait_for_terminal_event(
        self,
        request: ModelPatternBBrokerDispatchRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ModelPatternBBrokerTerminalEvent:  # pragma: no cover - never reached
        raise AssertionError(
            "wait_for_terminal_event() must not be reached via _missing_handle"
        )


@pytest.mark.parametrize(
    "stand_in_cls",
    [
        _PreFixAdapterPatternBBrokerPublishStandIn,
        _PreFixAdapterPatternBBrokerTerminalConsumerStandIn,
    ],
)
async def test_real_engine_dispatch_binds_missing_handle_pre_fix(
    stand_in_cls: type,
) -> None:
    """RED: real omnibase_infra dispatch machinery binds ``_missing_handle``
    for a handler exposing neither ``handle`` nor ``handle_async`` — the exact
    pre-fix shape of both Pattern B broker adapters."""
    instance = stand_in_cls(event_bus=object())
    assert not hasattr(instance, "handle")
    assert not hasattr(instance, "handle_async")

    callback = _make_dispatch_callback(instance, event_model=None)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=_make_dispatch_request()
    )

    with pytest.raises(ModelOnexError, match=re.escape(_MISSING_HANDLE_MESSAGE)):
        await callback(envelope)


@pytest.mark.unit
def test_adapter_pattern_b_broker_publish_exposes_dispatch_entrypoint() -> None:
    """GREEN: the real (fixed) publish adapter exposes handle()."""
    config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    adapter = AdapterPatternBBrokerPublish(event_bus=object(), config=config)  # type: ignore[arg-type]
    assert hasattr(adapter, "handle") or hasattr(adapter, "handle_async")


@pytest.mark.unit
def test_adapter_pattern_b_broker_terminal_consumer_exposes_dispatch_entrypoint() -> (
    None
):
    """GREEN: the real (fixed) terminal-consumer adapter exposes handle()."""
    config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    adapter = AdapterPatternBBrokerTerminalConsumer(
        event_bus=object(),  # type: ignore[arg-type]
        config=config,
    )
    assert hasattr(adapter, "handle") or hasattr(adapter, "handle_async")


@pytest.mark.asyncio
async def test_real_engine_dispatch_reaches_publish_via_handle_post_fix() -> None:
    """GREEN: driving the exact real dispatch machinery — resolving
    ``event_model`` via the real contract-discovery path used by production
    auto-wiring, not a hand-typed stand-in — reaches ``publish()`` through
    ``handle()`` and returns the real typed receipt. No ``ModelOnexError``,
    no ``_missing_handle``.
    """
    manifest = discover_contracts_from_paths([_BROKER_CONTRACT])
    (contract,) = [c for c in manifest.contracts if c.name == "pattern_b_broker"]
    (publish_entry,) = [
        e
        for e in contract.handler_routing.handlers
        if e.operation == "pattern_b_broker_publish"
    ]
    assert publish_entry.event_model is not None, (
        "contract must declare event_model for the publish operation so "
        "auto-wiring hands handle() a typed payload, not a raw envelope"
    )

    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        callback = _make_dispatch_callback(adapter, publish_entry.event_model)
        request = _make_dispatch_request()
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(payload=request)

        result = await callback(envelope)

        history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic
        )
        assert len(history) == 1, (
            "handle() must have delegated to publish() and actually "
            f"published to the bus; dispatch result={result!r}"
        )
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_real_engine_dispatch_reaches_wait_for_terminal_event_via_handle_post_fix() -> (
    None
):
    """GREEN: same real-engine proof for the terminal-consumer adapter."""
    manifest = discover_contracts_from_paths([_BROKER_CONTRACT])
    (contract,) = [c for c in manifest.contracts if c.name == "pattern_b_broker"]
    (consumer_entry,) = [
        e
        for e in contract.handler_routing.handlers
        if e.operation == "pattern_b_broker_terminal_consumer"
    ]
    assert consumer_entry.event_model is not None

    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)
        callback = _make_dispatch_callback(adapter, consumer_entry.event_model)
        # Short wait_policy timeout so this test observes the real timeout
        # branch of wait_for_terminal_event() in ~1s instead of the 300s
        # contract default — no terminal event is published on this bus.
        request = _make_dispatch_request(wait_timeout_seconds=1)
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(payload=request)

        result = await callback(envelope)
    finally:
        await bus.close()

    # handle() reached the real wait_for_terminal_event() domain method
    # (rather than raising _missing_handle's ModelOnexError) and the runtime
    # normalized its typed return into a dispatch result.
    assert result is not None
