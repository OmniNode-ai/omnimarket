"""Golden chain tests for node_build_dispatch_effect.

Verifies delegation payload construction, dry-run mode, duplicate rejection,
and event bus wiring. Uses EventBusInmemory, zero infra required.

Behavior change (OMN-7582): filesystem fallback tests REMOVED — Kafka is
the canonical transport.

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
    _DELEGATION_EVENT_TYPE,
    HandlerBuildDispatch,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    EnumBuildability,
    ModelBuildTarget,
)

CMD_TOPIC = "onex.cmd.omnimarket.build-loop-build.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.build-dispatch-completed.v1"
DELEGATION_TOPIC = "onex.cmd.omnimarket.delegation-request.v1"


def _target(ticket_id: str = "OMN-1234", title: str = "Fix widget") -> ModelBuildTarget:
    return ModelBuildTarget(
        ticket_id=ticket_id,
        title=title,
        buildability=EnumBuildability.AUTO_BUILDABLE,
    )


# ------------------------------------------------------------------
# Delegation payload path (primary — orchestrator publishes)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestDelegationPayloads:
    """Tests for the primary delegation payload path."""

    async def test_builds_delegation_payload(self) -> None:
        handler = HandlerBuildDispatch()

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(_target(),),
        )

        assert result.total_dispatched == 1
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 1

        dp = result.delegation_payloads[0]
        assert dp.event_type == _DELEGATION_EVENT_TYPE
        assert dp.topic == DELEGATION_TOPIC
        assert "OMN-1234" in dp.payload["prompt"]

    async def test_builds_multiple_payloads(self) -> None:
        handler = HandlerBuildDispatch()

        targets = (
            _target("OMN-1001", "First"),
            _target("OMN-1002", "Second"),
            _target("OMN-1003", "Third"),
        )
        result = await handler.handle(correlation_id=uuid4(), targets=targets)

        assert result.total_dispatched == 3
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 3

    async def test_payload_shape(self) -> None:
        """Ensure the delegation payload matches ModelDelegationRequest fields."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(correlation_id=cid, targets=(_target(),))

        dp = result.delegation_payloads[0]
        assert dp.payload["task_type"] == "research"
        assert dp.payload["correlation_id"] == str(cid)
        assert dp.payload["max_tokens"] == 4096
        assert "emitted_at" in dp.payload
        assert dp.correlation_id == cid


# ------------------------------------------------------------------
# Dry-run
# ------------------------------------------------------------------


@pytest.mark.unit
class TestDryRun:
    async def test_dry_run_skips_payload_build(self) -> None:
        handler = HandlerBuildDispatch()

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(_target(),),
            dry_run=True,
        )

        assert result.total_dispatched == 1
        assert len(result.delegation_payloads) == 0

    async def test_dry_run_empty_targets(self) -> None:
        handler = HandlerBuildDispatch()

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(),
            dry_run=True,
        )

        assert result.total_dispatched == 0
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 0


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


@pytest.mark.unit
class TestValidation:
    async def test_duplicate_ticket_ids_rejected(self) -> None:
        handler = HandlerBuildDispatch()

        with pytest.raises(ValueError, match="Duplicate"):
            await handler.handle(
                correlation_id=uuid4(),
                targets=(_target("OMN-1001"), _target("OMN-1001")),
            )

    async def test_empty_targets_returns_zero_counts(self) -> None:
        handler = HandlerBuildDispatch()

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(),
        )

        assert result.total_dispatched == 0
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 0
        assert len(result.outcomes) == 0


# ------------------------------------------------------------------
# Handler properties
# ------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerProperties:
    def test_handler_type(self) -> None:
        handler = HandlerBuildDispatch()
        assert handler.handler_type == "node_handler"

    def test_handler_category(self) -> None:
        handler = HandlerBuildDispatch()
        assert handler.handler_category == "effect"


# ------------------------------------------------------------------
# Event bus wiring
# ------------------------------------------------------------------


@pytest.mark.unit
class TestEventBusWiring:
    async def test_delegation_payloads_publishable(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Delegation payloads can be serialized and published to EventBusInmemory."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(
            correlation_id=cid,
            targets=(_target(),),
        )

        await event_bus.start()

        for dp in result.delegation_payloads:
            payload_bytes = json.dumps(dp.payload).encode()
            await event_bus.publish(
                dp.topic,
                key=None,
                value=payload_bytes,
            )

        history = await event_bus.get_event_history(topic=DELEGATION_TOPIC)
        assert len(history) == 1

        deserialized = json.loads(history[0].value)
        assert deserialized["task_type"] == "research"
        assert deserialized["correlation_id"] == str(cid)

        await event_bus.close()
