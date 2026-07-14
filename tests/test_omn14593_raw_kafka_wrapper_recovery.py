"""OMN-14593: node_projection_registration raw-kafka-wrapper recovery.

Root cause (confirmed live on .201, 2026-07-13 cold-boot chain-peel): the
shared ``MessageDispatchEngine`` fans a single ``dispatch(topic, envelope)``
call out to EVERY dispatcher registered for that topic, not just the
consumer group whose callback made the call (see
``omnibase_infra/src/omnibase_infra/runtime/message_dispatch_engine.py``,
``dispatch()`` docstring: "Execute dispatchers (fan-out)"). Three of this
node's topics (``node-introspection.v1`` / ``node-heartbeat.v1`` /
``node-state-change.v1``) are ALSO subscribed by a sibling raw/audit-purpose
consumer (``node_ledger_projection_compute``, ``event_bus.consumer_purpose:
audit``). That sibling's callback
(``_make_raw_event_projection_callback``) builds its envelope as
``payload=ModelEventMessage.model_dump(mode="json")`` — the undecoded kafka
record (topic/key/value/headers/offset/partition) — and its OWN dispatch()
call reaches this handler's dispatcher too, handing it that raw shape
instead of the decoded domain event. Every message of this shape previously
raised a 10-field ``ValidationError`` against ``ModelNodeIntrospectionEvent``
/ ``ModelNodeHeartbeatEvent`` and was dropped — no ``dlq_topics`` was
declared, so the drop was permanent, not recoverable.

These tests drive the REAL protocol entrypoint (``HandlerProjectionRegistration
.handle()``, the RuntimeLocal dispatch shim), not ``project_introspection()``/
``project_heartbeat()`` directly — the golden-chain/heartbeat-mechanism tests
call those directly and would pass even with this defect present, since it
lives entirely in ``handle()``'s payload extraction (feedback:
test_the_artifact_that_runs).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.models.registration.model_node_heartbeat_event import (
    ModelNodeHeartbeatEvent,
)
from omnibase_infra.models.registration.model_node_introspection_event import (
    ModelNodeIntrospectionEvent,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    HandlerProjectionRegistration,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionRegistration()


def _raw_kafka_wrapper_for(
    payload_model, *, topic: str, event_type: str
) -> dict[str, object]:
    """Build the EXACT poisoned shape the shared dispatch engine's fan-out produces.

    Mirrors ``_make_raw_event_projection_callback``: wraps ``payload_model`` in a
    real ``ModelEventEnvelope`` (matching ``mixin_kafka_broadcast.publish_envelope``'s
    wire format), stuffs the serialized envelope into a ``ModelEventMessage.value``,
    then dumps THAT — reproducing the raw record dict this handler incorrectly
    received instead of the decoded domain event.
    """
    correlation_id = getattr(payload_model, "correlation_id", None) or uuid4()
    envelope = ModelEventEnvelope(payload=payload_model, correlation_id=correlation_id)
    wire_value = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
    message = ModelEventMessage(
        topic=topic,
        key=None,
        value=wire_value,
        headers=ModelEventHeaders(
            source="node_ledger_projection_compute",
            event_type=event_type,
            content_type="application/json",
            timestamp=datetime.now(UTC),
            correlation_id=correlation_id,
        ),
        offset="16",
        partition=0,
    )
    return message.model_dump(mode="json")


class TestRawKafkaWrapperRecovery:
    """handle() must recover the real event from a cross-contaminated dispatch."""

    def test_introspection_recovers_from_raw_wrapper(self) -> None:
        db = InmemoryDatabaseAdapter()
        intro = ModelNodeIntrospectionEvent(
            node_id=uuid4(),
            node_name="node_omn14593_intro",
            node_type=EnumNodeKind.EFFECT,
            node_version=ModelSemVer(major=1, minor=0, patch=0),
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
        )
        wrapper = _raw_kafka_wrapper_for(
            intro,
            topic="onex.evt.platform.node-introspection.v1",
            event_type="node-introspection",
        )
        # Confirm the input really is the poisoned shape (topic/partition present,
        # node_id absent) before asserting recovery -- otherwise this test would
        # pass vacuously.
        assert "topic" in wrapper
        assert "partition" in wrapper
        assert "node_id" not in wrapper

        input_data: dict[str, object] = {
            **wrapper,
            "_db": db,
            "_event_type": "introspection",
        }
        result = HANDLER.handle(input_data)
        assert result["rows_upserted"] == 1

        rows = db.query("node_service_registry")
        assert len(rows) == 1
        assert rows[0]["service_name"] == "node_omn14593_intro"

    def test_heartbeat_recovers_from_raw_wrapper(self) -> None:
        db = InmemoryDatabaseAdapter()
        node_id = uuid4()
        intro = ModelNodeIntrospectionEvent(
            node_id=node_id,
            node_name="node_omn14593_hb",
            node_type=EnumNodeKind.EFFECT,
            node_version=ModelSemVer(major=1, minor=0, patch=0),
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
        )
        HANDLER.project_introspection(intro, db)

        hb = ModelNodeHeartbeatEvent(
            node_id=node_id,
            node_type=EnumNodeKind.EFFECT,
            node_version=ModelSemVer(major=1, minor=0, patch=0),
            uptime_seconds=42,
            timestamp=datetime.now(tz=UTC),
        )
        wrapper = _raw_kafka_wrapper_for(
            hb,
            topic="onex.evt.platform.node-heartbeat.v1",
            event_type="node-heartbeat",
        )
        assert "topic" in wrapper
        assert "node_id" not in wrapper

        input_data: dict[str, object] = {
            **wrapper,
            "_db": db,
            "_event_type": "heartbeat",
        }
        result = HANDLER.handle(input_data)
        assert result["rows_upserted"] == 1

        rows = db.query("node_service_registry")
        assert rows[0]["uptime_seconds"] == 42

    def test_unrecoverable_wrapper_raises_clear_error_not_silent_drop(self) -> None:
        """A wrapper whose 'value' is garbage must fail loud, not vanish quietly."""
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "topic": "onex.evt.platform.node-introspection.v1",
            "partition": 0,
            "offset": "16",
            "key": None,
            "headers": {},
            "value": "not valid json {{{",
            "_db": db,
            "_event_type": "introspection",
        }
        with pytest.raises(ValueError, match="OMN-14593"):
            HANDLER.handle(input_data)

    def test_normal_decoded_payload_unaffected(self) -> None:
        """The fix must not change behavior for the CORRECT (already-decoded) path."""
        db = InmemoryDatabaseAdapter()
        intro = ModelNodeIntrospectionEvent(
            node_id=uuid4(),
            node_name="node_omn14593_normal",
            node_type=EnumNodeKind.EFFECT,
            node_version=ModelSemVer(major=1, minor=0, patch=0),
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
        )
        input_data: dict[str, object] = {
            **intro.model_dump(mode="json"),
            "_db": db,
            "_event_type": "introspection",
        }
        result = HANDLER.handle(input_data)
        assert result["rows_upserted"] == 1

    def test_pre_fix_reproduction_would_have_raised_pydantic_validation_error(
        self,
    ) -> None:
        """Documents the EXACT failure this ticket fixes (ticket's verbatim evidence).

        Directly validating the raw wrapper against the canonical model (bypassing
        the handler's recovery) must still raise -- proves the poisoned shape is
        genuinely invalid input, not a fixture artifact, and pins the regression
        this handler now guards against.
        """
        intro = ModelNodeIntrospectionEvent(
            node_id=uuid4(),
            node_name="node_omn14593_repro",
            node_type=EnumNodeKind.EFFECT,
            node_version=ModelSemVer(major=1, minor=0, patch=0),
            correlation_id=uuid4(),
            timestamp=datetime.now(tz=UTC),
        )
        wrapper = _raw_kafka_wrapper_for(
            intro,
            topic="onex.evt.platform.node-introspection.v1",
            event_type="node-introspection",
        )
        with pytest.raises(ValidationError):
            ModelNodeIntrospectionEvent.model_validate(wrapper)
