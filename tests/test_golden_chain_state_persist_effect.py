# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_state_persist_effect.

Verifies the full reducer → intent → effect → state store → event round-trip
using EventBusInmemory and a mock ProtocolStateStore (zero infra required).

Test coverage:
- Happy path: store.put() called, success event emitted
- Failure path: store.put() raises, success=False event emitted
- No store injected: no-op success (dry-run semantics)
- Input model is frozen (immutable)
- Result model is frozen (immutable)
- Full EventBusInmemory round-trip (subscribe → publish → confirm)
- Integration: ModelPersistStateIntent → handler → ModelStatePersistedEvent
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.intents import ModelPersistStateIntent
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.nodes.node_state_persist_effect.handlers.handler_state_persist_effect import (
    HandlerStatePersistEffect,
)
from omnimarket.nodes.node_state_persist_effect.models.model_state_persist_input import (
    ModelStatePersistInput,
)
from omnimarket.nodes.node_state_persist_effect.models.model_state_persisted_event import (
    ModelStatePersistedEvent,
)

SUBSCRIBE_TOPIC = "onex.intent.omnimarket.state-persist.v1"
PUBLISH_TOPIC = "onex.evt.omnimarket.state-persisted.v1"


# ---------------------------------------------------------------------------
# Mock store
# ---------------------------------------------------------------------------


class MockStateStore:
    """In-memory mock for ProtocolStateStore.

    Records all put() calls for assertion in tests.
    Can be configured to raise on the next call.
    """

    def __init__(self, should_fail: bool = False) -> None:
        self._should_fail = should_fail
        self.put_calls: list[ModelStateEnvelope] = []

    async def put(self, envelope: ModelStateEnvelope) -> None:
        if self._should_fail:
            msg = "Mock state store write failure"
            raise RuntimeError(msg)
        self.put_calls.append(envelope)

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        for env in reversed(self.put_calls):
            if env.node_id == node_id and env.scope_id == scope_id:
                return env
        return None

    async def delete(self, node_id: str, scope_id: str = "default") -> bool:
        before = len(self.put_calls)
        self.put_calls = [
            e
            for e in self.put_calls
            if not (e.node_id == node_id and e.scope_id == scope_id)
        ]
        return len(self.put_calls) < before

    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        return any(
            e.node_id == node_id and e.scope_id == scope_id for e in self.put_calls
        )

    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        pairs = [(e.node_id, e.scope_id) for e in self.put_calls]
        if node_id is not None:
            pairs = [(n, s) for n, s in pairs if n == node_id]
        return sorted(set(pairs))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_envelope(node_id: str = "node-ledger") -> ModelStateEnvelope:
    return ModelStateEnvelope(
        node_id=node_id,
        scope_id="default",
        data={"sweep_count": 42, "last_run": "2026-04-16T00:00:00Z"},
        written_at=datetime.now(UTC),
        contract_fingerprint="fingerprint-abc",
    )


def _make_input(
    envelope: ModelStateEnvelope | None = None,
) -> ModelStatePersistInput:
    env = envelope or _make_envelope()
    return ModelStatePersistInput(
        correlation_id=uuid4(),
        intent_id=uuid4(),
        envelope=env,
        emitted_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatePersistEffectGoldenChain:
    """Golden chain: state persist effect with protocol-based DI."""

    async def test_happy_path_store_called(self, event_bus: EventBusInmemory) -> None:
        """Store.put() is called, success event returned with persisted_at set."""
        store = MockStateStore()
        handler = HandlerStatePersistEffect(state_store=store)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id,
            intent_id=inp.intent_id,
            envelope=inp.envelope,
            emitted_at=inp.emitted_at,
        )

        assert result.success is True
        assert result.intent_id == inp.intent_id
        assert result.persisted_at is not None
        assert result.error is None
        assert len(store.put_calls) == 1
        assert store.put_calls[0].node_id == inp.envelope.node_id

    async def test_failure_path_store_raises(self, event_bus: EventBusInmemory) -> None:
        """Store.put() raises -> success=False, error message populated."""
        store = MockStateStore(should_fail=True)
        handler = HandlerStatePersistEffect(state_store=store)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id,
            intent_id=inp.intent_id,
            envelope=inp.envelope,
            emitted_at=inp.emitted_at,
        )

        assert result.success is False
        assert result.intent_id == inp.intent_id
        assert result.persisted_at is None
        assert result.error is not None
        assert "Mock state store write failure" in result.error

    async def test_no_store_injected_noop_success(
        self, event_bus: EventBusInmemory
    ) -> None:
        """No state store injected -> no-op success (dry-run semantics)."""
        handler = HandlerStatePersistEffect()
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id,
            intent_id=inp.intent_id,
            envelope=inp.envelope,
            emitted_at=inp.emitted_at,
        )

        assert result.success is True
        assert result.persisted_at is not None
        assert result.error is None

    async def test_result_model_frozen(self, event_bus: EventBusInmemory) -> None:
        """ModelStatePersistedEvent is frozen (immutable)."""
        handler = HandlerStatePersistEffect()
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id,
            intent_id=inp.intent_id,
            envelope=inp.envelope,
            emitted_at=inp.emitted_at,
        )

        with pytest.raises(Exception, match="frozen"):
            result.success = False  # type: ignore[misc]

    async def test_input_model_frozen(self, event_bus: EventBusInmemory) -> None:
        """ModelStatePersistInput is frozen (immutable)."""
        inp = _make_input()

        with pytest.raises(Exception, match="frozen"):
            inp.intent_id = uuid4()  # type: ignore[misc]

    async def test_intent_id_echoed_in_result(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Result echoes the input intent_id for correlation traceability."""
        store = MockStateStore()
        handler = HandlerStatePersistEffect(state_store=store)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id,
            intent_id=inp.intent_id,
            envelope=inp.envelope,
            emitted_at=inp.emitted_at,
        )

        assert result.intent_id == inp.intent_id

    async def test_event_bus_round_trip(self, event_bus: EventBusInmemory) -> None:
        """Full EventBusInmemory round-trip: publish intent → handler → emit event."""
        store = MockStateStore()
        handler = HandlerStatePersistEffect(state_store=store)
        published_events: list[dict[str, object]] = []
        event_seen = asyncio.Event()

        async def on_intent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[attr-defined]
            envelope = ModelStateEnvelope(**payload["envelope"])
            result = await handler.handle(
                correlation_id=payload["correlation_id"],
                intent_id=payload["intent_id"],
                envelope=envelope,
                emitted_at=datetime.fromisoformat(payload["emitted_at"]),
            )
            result_payload = result.model_dump(mode="json")
            published_events.append(result_payload)
            await event_bus.publish(
                PUBLISH_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )
            event_seen.set()

        await event_bus.start()
        await event_bus.subscribe(
            SUBSCRIBE_TOPIC,
            on_message=on_intent,
            group_id="test-state-persist-effect",
        )

        inp = _make_input()
        intent_payload = json.dumps(
            {
                "correlation_id": str(inp.correlation_id),
                "intent_id": str(inp.intent_id),
                "envelope": inp.envelope.model_dump(mode="json"),
                "emitted_at": inp.emitted_at.isoformat(),
            }
        ).encode()

        try:
            await event_bus.publish(SUBSCRIBE_TOPIC, key=None, value=intent_payload)
            await asyncio.wait_for(event_seen.wait(), timeout=5.0)

            assert len(published_events) == 1
            assert published_events[0]["success"] is True

            history = await event_bus.get_event_history(topic=PUBLISH_TOPIC)
            assert len(history) == 1
        finally:
            await event_bus.close()

    async def test_reducer_intent_to_effect_round_trip(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Integration: ModelPersistStateIntent -> handler -> ModelStatePersistedEvent.

        Simulates the full reducer → effect pipeline:
        1. Build ModelPersistStateIntent (as a reducer would)
        2. Pass fields to the handler (as the runtime would)
        3. Assert ModelStatePersistedEvent confirms the write
        4. Verify the mock store actually received the envelope
        """
        store = MockStateStore()
        handler = HandlerStatePersistEffect(state_store=store)

        # Step 1: Build the intent (simulating reducer output)
        envelope = _make_envelope(node_id="node-handler-ledger")
        intent = ModelPersistStateIntent(
            intent_id=uuid4(),
            envelope=envelope,
            emitted_at=datetime.now(UTC),
            correlation_id=uuid4(),
        )

        # Step 2: Handler receives intent fields (as runtime would dispatch)
        result = await handler.handle(
            correlation_id=intent.correlation_id,
            intent_id=intent.intent_id,
            envelope=intent.envelope,
            emitted_at=intent.emitted_at,
        )

        # Step 3: Confirm the event
        assert isinstance(result, ModelStatePersistedEvent)
        assert result.success is True
        assert result.intent_id == intent.intent_id
        assert result.persisted_at is not None
        assert result.error is None

        # Step 4: Verify store received the exact envelope
        assert len(store.put_calls) == 1
        persisted = store.put_calls[0]
        assert persisted.node_id == "node-handler-ledger"
        assert persisted.data["sweep_count"] == 42
        assert persisted.contract_fingerprint == "fingerprint-abc"

    async def test_multiple_successive_puts(self, event_bus: EventBusInmemory) -> None:
        """Handler can persist multiple envelopes in sequence."""
        store = MockStateStore()
        handler = HandlerStatePersistEffect(state_store=store)

        for i in range(3):
            envelope = ModelStateEnvelope(
                node_id=f"node-{i}",
                data={"index": i},
                written_at=datetime.now(UTC),
            )
            inp = ModelStatePersistInput(
                correlation_id=uuid4(),
                intent_id=uuid4(),
                envelope=envelope,
                emitted_at=datetime.now(UTC),
            )
            result = await handler.handle(
                correlation_id=inp.correlation_id,
                intent_id=inp.intent_id,
                envelope=inp.envelope,
                emitted_at=inp.emitted_at,
            )
            assert result.success is True

        assert len(store.put_calls) == 3
        node_ids = [e.node_id for e in store.put_calls]
        assert node_ids == ["node-0", "node-1", "node-2"]
