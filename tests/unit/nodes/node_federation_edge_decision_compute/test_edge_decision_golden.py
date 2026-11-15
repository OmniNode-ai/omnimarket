# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden chain of the federation edge decision: one native record passes.

``overlay.example.yaml``'s allowed-topic event, recorded natively and
unstamped, passes: the answer's record is the pinned physical topic with the
key and value base64 strings EXACTLY as they came in (decoding them gives the
input bytes) and the input headers followed by the overlay's three stamp
headers (origin lane, source lane, first hop), with nothing else selected.
A recording that also holds two events on other topics passes with
``not_selected`` 2 and the other two never reaching the record. Property:
fifty generated uuid records each pass with the output bytes equal to the
input bytes. Determinism: the same request decides the same twice, and the
decision's JSON never carries the key text or the value text — only their
base64 forms inside ``record_out``.
"""

from __future__ import annotations

import base64
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.federation_egress.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
    ModelWireHeader,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_federation_edge_decision import (
    FIRST_HOP,
    HandlerFederationEdgeDecision,
    decide_edge,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelAclProbeRecord,
    ModelAclProbeStep,
    ModelEdgeDecisionRequest,
    ModelLaneDeclaration,
    ModelRecordedEvent,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute/overlay.example.yaml"
)

# The fixed decision moment; the handler reads no clock, so ``now`` is input.
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)

# One fixed uuid string: the recorded event's command_id and key bytes.
COMMAND_ID = "0f7e1a9c-5b2d-4c8e-a1d3-9f6b2e7c4a50"

# The message-id value: a uuid derived from COMMAND_ID, so it fits the
# overlay's ``^[0-9a-f-]{36}$`` header rule and never equals the key text.
MESSAGE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, COMMAND_ID))

# The connection's mechanism, as the lane declaration declares it.
MECHANISM = "example-mechanism"

_OVERLAY: ModelEdgeOverlayFile | None = None


def _overlay() -> ModelEdgeOverlayFile:
    """The example overlay, loaded once and shared (frozen, so immutable)."""
    global _OVERLAY
    if _OVERLAY is None:
        document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
        _OVERLAY = ModelEdgeOverlayFile.model_validate(document)
    return _OVERLAY


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _value_bytes(command_id: str) -> bytes:
    """The event's raw value bytes: one JSON object naming the command."""
    return b'{"command_id":"' + command_id.encode("ascii") + b'"}'


def _event(**overrides: Any) -> ModelRecordedEvent:
    """The native unstamped recording of one event on the allowed topic."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "key_b64": _b64(COMMAND_ID.encode("ascii")),
        "value_b64": _b64(_value_bytes(COMMAND_ID)),
        "headers": (
            ModelWireHeader(name=direction.message_id_header, value=MESSAGE_ID),
        ),
    }
    values.update(overrides)
    return ModelRecordedEvent(**values)


def _probe(**overrides: Any) -> ModelAclProbeRecord:
    """This direction's fresh probe: a b allowed, c d e denied, 60s old."""
    to = _overlay().direction.to
    values: dict[str, Any] = {
        "probe_ref": to.acl_probe_ref,
        "lane": to.lane,
        "principal_ref": _overlay().binding.principal_ref,
        "recorded_at": NOW - timedelta(seconds=60),
        "steps": (
            ModelAclProbeStep(step="a", expected="allowed", observed="allowed"),
            ModelAclProbeStep(step="b", expected="allowed", observed="allowed"),
            ModelAclProbeStep(step="c", expected="denied", observed="denied"),
            ModelAclProbeStep(step="d", expected="denied", observed="denied"),
            ModelAclProbeStep(step="e", expected="denied", observed="denied"),
        ),
    }
    values.update(overrides)
    return ModelAclProbeRecord(**values)


def _registry_entry(**overrides: Any) -> ModelRegistryTopicEntry:
    """The allowed topic's registry entry: internal, closed one-field shape."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "classes": ("internal",),
        "atomic_group": False,
        "fields": (
            ModelEgressField(
                name=direction.key_binding,
                kind="string",
                required=True,
                pattern=direction.key_pattern,
                max_length=36,
            ),
        ),
    }
    values.update(overrides)
    return ModelRegistryTopicEntry(**values)


def _lane_declaration(**overrides: Any) -> ModelLaneDeclaration:
    """The destination lane as the deployment declares it."""
    values: dict[str, Any] = {
        "lane": _overlay().direction.to.lane,
        "auth_mechanism": MECHANISM,
        "granted_write_principals": (_overlay().binding.principal_ref,),
    }
    values.update(overrides)
    return ModelLaneDeclaration(**values)


def _request(**overrides: Any) -> ModelEdgeDecisionRequest:
    """The one passing request, with whole-field ``overrides`` applied."""
    values: dict[str, Any] = {
        "recording": (_event(),),
        "overlay": _overlay(),
        "authenticated_identity": _overlay().binding.principal_ref,
        "connection_mechanism": MECHANISM,
        "lane_declaration": _lane_declaration(),
        "registry_entry": _registry_entry(),
        "acl_probe": _probe(),
        "now": NOW,
    }
    values.update(overrides)
    return ModelEdgeDecisionRequest(**values)


def _handler() -> HandlerFederationEdgeDecision:
    return HandlerFederationEdgeDecision()


class TestGoldenChain:
    def test_the_native_unstamped_record_passes(self) -> None:
        decision = _handler().handle(_request())
        assert decision.outcome is EnumEgressOutcome.PASS
        assert decision.reason is None
        assert decision.not_selected == 0
        overlay = _overlay()
        record = decision.record_out
        assert record is not None
        assert record.topic == overlay.direction.pinned_physical_topic
        assert decision.topic == overlay.direction.pinned_physical_topic
        event = _event()
        assert record.key_b64 == event.key_b64
        assert record.value_b64 == event.value_b64
        assert base64.b64decode(record.key_b64) == COMMAND_ID.encode("ascii")
        assert base64.b64decode(record.value_b64) == _value_bytes(COMMAND_ID)
        stamps = overlay.direction.stamp_headers
        origin_lane = overlay.binding.origin_lane
        assert record.headers == (
            *event.headers,
            ModelWireHeader(name=stamps.origin_lane, value=origin_lane),
            ModelWireHeader(name=stamps.source_lane, value=origin_lane),
            ModelWireHeader(name=stamps.hops, value=FIRST_HOP),
        )

    def test_other_topics_are_not_selected_and_never_reach_the_record(
        self,
    ) -> None:
        overlay = _overlay()
        side_topic_a = overlay.direction.receipt.record_topic
        side_topic_b = overlay.direction.receipt.refusal_topic
        recording = (
            _event(),
            ModelRecordedEvent(
                topic=side_topic_a, value_b64=_b64(b'{"synthetic":"a"}')
            ),
            ModelRecordedEvent(
                topic=side_topic_b, value_b64=_b64(b'{"synthetic":"b"}')
            ),
        )
        decision = _handler().handle(_request(recording=recording))
        assert decision.outcome is EnumEgressOutcome.PASS
        assert decision.not_selected == 2
        record = decision.record_out
        assert record is not None
        assert record.topic == overlay.direction.pinned_physical_topic
        assert record.key_b64 == recording[0].key_b64
        assert record.value_b64 == recording[0].value_b64
        assert record.value_b64 != recording[1].value_b64
        assert record.value_b64 != recording[2].value_b64
        assert record.key_b64 not in (recording[1].key_b64, recording[2].key_b64)

    def test_fifty_generated_uuid_records_each_pass_byte_identical(self) -> None:
        rng = random.Random(11)
        overlay = _overlay()
        for index in range(50):
            command_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            value = _value_bytes(command_id)
            event = ModelRecordedEvent(
                topic=overlay.direction.allowed_topic,
                key_b64=_b64(command_id.encode("ascii")),
                value_b64=_b64(value),
                headers=(
                    ModelWireHeader(
                        name=overlay.direction.message_id_header, value=command_id
                    ),
                ),
            )
            decision = _handler().handle(_request(recording=(event,)))
            assert decision.outcome is EnumEgressOutcome.PASS, index
            assert decision.not_selected == 0, index
            assert decision.message_id == command_id, index
            record = decision.record_out
            assert record is not None, index
            assert record.key_b64 == event.key_b64, index
            assert record.value_b64 == event.value_b64, index
            assert base64.b64decode(record.key_b64) == command_id.encode("ascii")
            assert base64.b64decode(record.value_b64) == value

    def test_the_same_request_decides_the_same_twice(self) -> None:
        request = _request()
        assert (
            _handler().handle(request).model_dump_json()
            == _handler().handle(request).model_dump_json()
        )
        assert (
            decide_edge(request).model_dump_json()
            == _handler().handle(_request()).model_dump_json()
        )

    def test_the_decision_json_never_carries_the_key_or_value_text(self) -> None:
        decision = _handler().handle(_request())
        text = decision.model_dump_json()
        value_text = _value_bytes(COMMAND_ID).decode("ascii")
        assert COMMAND_ID not in text
        assert value_text not in text
        assert '"command_id"' not in text
