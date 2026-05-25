"""Golden chain tests for node_build_dispatch_effect.

Verifies ticket-pipeline-start payload construction, dry-run mode,
duplicate rejection, and event bus wiring. Uses EventBusInmemory,
zero infra required.

Behavior change (OMN-7582): filesystem fallback tests REMOVED — Kafka is
the canonical transport.

Behavior change (OMN-7720): dispatch target changed from delegation-request
to ticket-pipeline-start. The build loop now dispatches a proper
ModelPipelineStartCommand-shaped payload directly to the ticket-pipeline
consumer, not a delegation-request for LLM routing.

Related:
    - OMN-7720: Build loop dispatches real ticket-pipeline worker via Kafka
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
    _TICKET_PIPELINE_EVENT_TYPE,
    HandlerBuildDispatch,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    EnumBuildability,
    ModelBuildTarget,
)

CMD_TOPIC = "onex.cmd.omnimarket.build-loop-build.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.build-dispatch-completed.v1"
TICKET_PIPELINE_START_TOPIC = "onex.cmd.omnimarket.ticket-pipeline-start.v1"


def _target(ticket_id: str = "OMN-1234", title: str = "Fix widget") -> ModelBuildTarget:
    return ModelBuildTarget(
        ticket_id=ticket_id,
        title=title,
        buildability=EnumBuildability.AUTO_BUILDABLE,
    )


# ------------------------------------------------------------------
# Ticket-pipeline start payload path (primary — orchestrator publishes)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestTicketPipelineStartPayloads:
    """Tests for the primary ticket-pipeline dispatch path (OMN-7720)."""

    async def test_builds_pipeline_start_payload(self) -> None:
        """Dispatch produces a ticket-pipeline-start payload, not delegation-request."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(
            correlation_id=cid,
            targets=(_target(),),
        )

        assert result.total_dispatched == 1
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 1

        dp = result.delegation_payloads[0]
        assert dp.event_type == _TICKET_PIPELINE_EVENT_TYPE
        assert dp.topic == TICKET_PIPELINE_START_TOPIC

    async def test_payload_contains_ticket_id(self) -> None:
        """Payload carries the ticket_id field required by ModelPipelineStartCommand."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(
            correlation_id=cid,
            targets=(_target("OMN-9999"),),
        )

        dp = result.delegation_payloads[0]
        assert dp.payload["ticket_id"] == "OMN-9999"

    async def test_payload_shape_matches_pipeline_start_command(self) -> None:
        """Payload carries all fields ModelPipelineStartCommand requires."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(correlation_id=cid, targets=(_target(),))

        dp = result.delegation_payloads[0]
        payload = dp.payload

        # Required fields for ModelPipelineStartCommand
        assert payload["ticket_id"] == "OMN-1234"
        assert payload["correlation_id"] == str(cid)
        assert "requested_at" in payload
        assert payload["dry_run"] is False
        assert payload["skip_test_iterate"] is False
        assert dp.correlation_id == cid

    async def test_pipeline_start_topic_not_delegation_request(self) -> None:
        """Explicitly assert the delegation-request topic is NOT used (OMN-7720)."""
        handler = HandlerBuildDispatch()

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(_target(),),
        )

        for dp in result.delegation_payloads:
            assert "delegation-request" not in dp.topic, (
                f"Build dispatch published to delegation-request ({dp.topic!r}) "
                "instead of ticket-pipeline-start — OMN-7720 regression"
            )

    async def test_builds_multiple_payloads(self) -> None:
        """One payload per target ticket, each targeting ticket-pipeline-start."""
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

        ticket_ids = {dp.payload["ticket_id"] for dp in result.delegation_payloads}
        assert ticket_ids == {"OMN-1001", "OMN-1002", "OMN-1003"}

        for dp in result.delegation_payloads:
            assert dp.topic == TICKET_PIPELINE_START_TOPIC

    async def test_correlation_id_per_ticket_is_cycle_id(self) -> None:
        """All payloads share the cycle correlation_id."""
        handler = HandlerBuildDispatch()
        cycle_id = uuid4()

        result = await handler.handle(
            correlation_id=cycle_id,
            targets=(_target("OMN-2001"), _target("OMN-2002")),
        )

        for dp in result.delegation_payloads:
            assert UUID(str(dp.payload["correlation_id"])) == cycle_id
            assert dp.correlation_id == cycle_id


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
    async def test_pipeline_start_payloads_publishable(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Payloads publish to ticket-pipeline-start and are correctly shaped."""
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

        history = await event_bus.get_event_history(topic=TICKET_PIPELINE_START_TOPIC)
        assert len(history) == 1

        deserialized = json.loads(history[0].value)
        assert deserialized["ticket_id"] == "OMN-1234"
        assert deserialized["correlation_id"] == str(cid)

        await event_bus.close()

    async def test_topic_not_published_to_delegation_request(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Nothing is published to the old delegation-request topic (OMN-7720)."""
        handler = HandlerBuildDispatch()
        cid = uuid4()

        result = await handler.handle(
            correlation_id=cid,
            targets=(_target(),),
        )

        await event_bus.start()

        for dp in result.delegation_payloads:
            payload_bytes = json.dumps(dp.payload).encode()
            await event_bus.publish(dp.topic, key=None, value=payload_bytes)

        # delegation-request must NOT have received any messages
        old_topic = "onex.cmd.omnimarket.delegation-request.v1"
        delegation_history = await event_bus.get_event_history(topic=old_topic)
        assert len(delegation_history) == 0, (
            f"OMN-7720 regression: {len(delegation_history)} message(s) published to "
            f"{old_topic!r}; build dispatch must target ticket-pipeline-start"
        )

        await event_bus.close()
