# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The error chain of the federation edge decision: one named refusal per cause.

Every refusal of ``EnumEdgeRefusal`` with its exact name and no ``record_out``,
each against the positive control (the unmutated request passes): the
selection (an empty recording, zero and two events on the allowed topic), the
registry entry (another topic, no entry, a class besides internal), the
connection (no lane declaration, no declared mechanism, another lane
declared, a different or absent connection mechanism), the wire identity
against the binding (another name, none), the permitted lanes and the
resolved route (another tenant's pinned topic, a mapping change), the ACL
probe (unmeasured, another lane, principal or ref, a missing or duplicated
letter, an expected-allowed step observed denied, stale, an expected-denied
step observed allowed) and every gate refusal forwarded word for word. Order:
the selection count beats a bad key, the identity beats a stale probe, a
stale probe beats a bad value. Failing controls: with ``gate_egress`` in the
handler module replaced by an always-pass fake the customer-content records
pass and ``record_out`` carries their bytes -- the gate is what refuses; with
``resolve_physical_topic`` in the handler module replaced a route-mutated
request passes -- the route refusal needs the real resolver; and with nothing
replaced a wire identity equal to the binding passes where the overlay's
configured name would not -- the check reads the binding, never the overlay's
names.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_federation_edge_decision_compute.handlers import (
    handler_federation_edge_decision as edge_module,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_federation_edge_decision import (
    HandlerFederationEdgeDecision,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumEdgeRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelAclProbeRecord,
    ModelAclProbeStep,
    ModelEdgeDecision,
    ModelEdgeDecisionRequest,
    ModelLaneDeclaration,
    ModelRecordedEvent,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.model_egress_gate import (
    ModelEgressField,
    ModelEgressGateRequest,
    ModelEgressGateResult,
    ModelWireHeader,
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

# Another principal's name, never the binding's.
OTHER_PRINCIPAL = "example-edge-writer-other"

# A lane outside the binding's permitted lanes.
OTHER_LANE = "example-lane-other"

# Another probe's ref, never this direction's.
OTHER_PROBE_REF = "example-acl-probe-record-other"

# A uuid-shaped key that is not the event's command_id.
OTHER_UUID = "1e2d3c4b-5a69-4788-9abc-def012345678"

# The pinned physical topic of another tenant (its slug in the wire prefix).
OTHER_TENANT_PINNED = (
    "tenant-example-tenant-b.onex.cmd.omnimarket.lifecycle-start-requested.v1"
)

# The overlay's pinned topic with exactly one character changed (the v1 to v2).
PINNED_ONE_CHARACTER_CHANGED = (
    "tenant-example-tenant-a.onex.cmd.omnimarket.lifecycle-start-requested.v2"
)

# The marker member of the customer-content records the gate control feeds.
MARKER = "synthetic-customer-marker"

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


def _marker_value() -> bytes:
    """The extra-member value: the command plus one unknown member."""
    return (
        b'{"command_id":"'
        + COMMAND_ID.encode("ascii")
        + b'","note":"'
        + MARKER.encode("ascii")
        + b'"}'
    )


def _duplicate_member_value() -> bytes:
    """The duplicate-member value: one JSON object naming the command twice."""
    return (
        b'{"command_id":"'
        + COMMAND_ID.encode("ascii")
        + b'","command_id":"'
        + COMMAND_ID.encode("ascii")
        + b'"}'
    )


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


def _steps() -> tuple[ModelAclProbeStep, ...]:
    """The five probe letters: a b allowed, c d e denied, each as expected."""
    return (
        ModelAclProbeStep(step="a", expected="allowed", observed="allowed"),
        ModelAclProbeStep(step="b", expected="allowed", observed="allowed"),
        ModelAclProbeStep(step="c", expected="denied", observed="denied"),
        ModelAclProbeStep(step="d", expected="denied", observed="denied"),
        ModelAclProbeStep(step="e", expected="denied", observed="denied"),
    )


def _probe(**overrides: Any) -> ModelAclProbeRecord:
    """This direction's fresh probe: a b allowed, c d e denied, 60s old."""
    to = _overlay().direction.to
    values: dict[str, Any] = {
        "probe_ref": to.acl_probe_ref,
        "lane": to.lane,
        "principal_ref": _overlay().binding.principal_ref,
        "recorded_at": NOW - timedelta(seconds=60),
        "steps": _steps(),
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
    # model_construct: a pydantic after-validator re-runs on an overlay instance nested in the request, so a deliberately
    # inconsistent overlay (the control cases below) can only reach the handler this way; every value here is already typed.
    return ModelEdgeDecisionRequest.model_construct(**values)


def _handler() -> HandlerFederationEdgeDecision:
    return HandlerFederationEdgeDecision()


def _refuses(reason: EnumEdgeRefusal, **overrides: Any) -> ModelEdgeDecision:
    """The positive control first, then the refusal: the unmutated request
    passes, and the one built with ``overrides`` refuses with exactly
    ``reason`` and carries no record."""
    control = _handler().handle(_request())
    assert control.outcome is EnumEgressOutcome.PASS
    assert control.record_out is not None
    decision = _handler().handle(_request(**overrides))
    assert decision.outcome is EnumEgressOutcome.REFUSE
    assert decision.reason is reason
    assert decision.record_out is None
    return decision


def _overlay_with(**direction_update: Any) -> ModelEdgeOverlayFile:
    """The example overlay with its direction mutated by whole-field update.

    ``model_copy`` bypasses the overlay file's validators, which refuse each
    of these disagreements at load; the request carries the mutated overlay
    whole, and the decision is what answers for it.
    """
    overlay = _overlay()
    direction = overlay.direction.model_copy(update=direction_update)
    return overlay.model_copy(update={"direction": direction})


class TestUnitNotSingle:
    def test_an_empty_recording_is_unit_not_single(self) -> None:
        decision = _refuses(EnumEdgeRefusal.UNIT_NOT_SINGLE, recording=())
        assert decision.not_selected == 0
        assert decision.topic is None

    def test_no_event_on_the_allowed_topic_is_unit_not_single(self) -> None:
        overlay = _overlay()
        recording = (
            ModelRecordedEvent(
                topic=overlay.direction.receipt.record_topic,
                value_b64=_b64(b'{"synthetic":"a"}'),
            ),
            ModelRecordedEvent(
                topic=overlay.direction.receipt.refusal_topic,
                value_b64=_b64(b'{"synthetic":"b"}'),
            ),
        )
        decision = _refuses(EnumEdgeRefusal.UNIT_NOT_SINGLE, recording=recording)
        assert decision.not_selected == 2
        assert decision.topic is None

    def test_two_events_on_the_allowed_topic_are_unit_not_single(self) -> None:
        decision = _refuses(
            EnumEdgeRefusal.UNIT_NOT_SINGLE, recording=(_event(), _event())
        )
        assert decision.not_selected == 0
        assert decision.topic is None


class TestTopicNotAllowed:
    def test_a_registry_entry_for_another_topic_refuses(self) -> None:
        overlay = _overlay()
        decision = _refuses(
            EnumEdgeRefusal.TOPIC_NOT_ALLOWED,
            registry_entry=_registry_entry(
                topic=overlay.direction.receipt.record_topic
            ),
        )
        assert decision.topic == overlay.direction.allowed_topic


class TestClassNotDeclared:
    def test_no_registry_entry_refuses(self) -> None:
        _refuses(EnumEdgeRefusal.CLASS_NOT_DECLARED, registry_entry=None)

    def test_an_entry_class_besides_internal_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.CLASS_NOT_DECLARED,
            registry_entry=_registry_entry(classes=("tenant",)),
        )


class TestLaneUnauthenticated:
    def test_no_lane_declaration_refuses(self) -> None:
        _refuses(EnumEdgeRefusal.LANE_UNAUTHENTICATED, lane_declaration=None)

    def test_a_lane_declaring_no_mechanism_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.LANE_UNAUTHENTICATED,
            lane_declaration=_lane_declaration(auth_mechanism=None),
        )

    def test_a_lane_declaration_for_another_lane_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.LANE_UNAUTHENTICATED,
            lane_declaration=_lane_declaration(lane=OTHER_LANE),
        )

    def test_a_connection_mechanism_besides_the_declared_one_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.LANE_UNAUTHENTICATED,
            connection_mechanism="example-mechanism-other",
        )

    def test_no_connection_mechanism_refuses(self) -> None:
        _refuses(EnumEdgeRefusal.LANE_UNAUTHENTICATED, connection_mechanism=None)


class TestPrincipalBindingMismatch:
    def test_another_identity_refuses_while_the_overlay_names_the_binding(
        self,
    ) -> None:
        overlay = _overlay()
        # The overlay's configured principal names all equal the binding's,
        # so the refusal can only be the wire identity's own disagreement.
        assert overlay.direction.to.auth_ref == overlay.binding.principal_ref
        assert (
            overlay.direction.acl.write_principal_ref == overlay.binding.principal_ref
        )
        _refuses(
            EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH,
            authenticated_identity=OTHER_PRINCIPAL,
        )

    def test_no_authenticated_identity_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH,
            authenticated_identity=None,
        )


class TestLaneNotPermitted:
    def test_a_destination_lane_outside_permitted_lanes_refuses(self) -> None:
        # model_copy bypasses the overlay validator, which refuses at load a
        # direction.to.lane outside the binding's permitted_lanes; the request
        # carries the mutated overlay whole.
        overlay = _overlay()
        to = overlay.direction.to.model_copy(update={"lane": OTHER_LANE})
        direction = overlay.direction.model_copy(update={"to": to})
        mutated = overlay.model_copy(update={"direction": direction})
        assert OTHER_LANE not in mutated.binding.permitted_lanes
        _refuses(
            EnumEdgeRefusal.LANE_NOT_PERMITTED,
            overlay=mutated,
            lane_declaration=_lane_declaration(lane=OTHER_LANE),
        )


class TestRouteOutsideTenant:
    def test_another_tenants_pinned_topic_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
            overlay=_overlay_with(pinned_physical_topic=OTHER_TENANT_PINNED),
        )

    def test_a_mapping_change_refuses(self) -> None:
        # Under the identity mapping the pinned physical topic is the allowed
        # topic itself, which the tenant-prefixed pinned topic is not.
        _refuses(
            EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
            overlay=_overlay_with(wire_topic_mapping="identity"),
        )

    def test_a_pinned_topic_one_character_changed_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
            overlay=_overlay_with(pinned_physical_topic=PINNED_ONE_CHARACTER_CHANGED),
        )


class TestAclNotMeasured:
    def test_no_probe_refuses(self) -> None:
        _refuses(EnumEdgeRefusal.ACL_NOT_MEASURED, acl_probe=None)


class TestAclProbeMismatch:
    def test_another_lanes_probe_refuses(self) -> None:
        _refuses(EnumEdgeRefusal.ACL_PROBE_MISMATCH, acl_probe=_probe(lane=OTHER_LANE))

    def test_another_principals_probe_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_MISMATCH,
            acl_probe=_probe(principal_ref=OTHER_PRINCIPAL),
        )

    def test_another_probe_ref_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_MISMATCH,
            acl_probe=_probe(probe_ref=OTHER_PROBE_REF),
        )

    def test_a_missing_step_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_MISMATCH,
            acl_probe=_probe(steps=_steps()[:4]),
        )

    def test_a_duplicated_step_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_MISMATCH,
            acl_probe=_probe(steps=(*_steps(), _steps()[0])),
        )

    def test_an_expected_allowed_step_observed_denied_refuses(self) -> None:
        steps = _steps()
        denied_a = steps[0].model_copy(update={"observed": "denied"})
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_MISMATCH,
            acl_probe=_probe(steps=(denied_a, *steps[1:])),
        )


class TestAclProbeStale:
    def test_a_probe_past_its_max_age_refuses(self) -> None:
        max_age = _overlay().direction.to.acl_probe_max_age_seconds
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_STALE,
            acl_probe=_probe(recorded_at=NOW - timedelta(seconds=max_age + 60)),
        )


class TestAclProbeNotDenied:
    def test_step_c_observed_allowed_refuses(self) -> None:
        steps = _steps()
        allowed_c = steps[2].model_copy(update={"observed": "allowed"})
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_NOT_DENIED,
            acl_probe=_probe(steps=(*steps[:2], allowed_c, *steps[3:])),
        )

    def test_step_e_observed_allowed_refuses(self) -> None:
        steps = _steps()
        allowed_e = steps[4].model_copy(update={"observed": "allowed"})
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_NOT_DENIED,
            acl_probe=_probe(steps=(*steps[:4], allowed_e)),
        )


class TestGateReasonsForwarded:
    """Every gate refusal arrives at the edge with its own name."""

    def test_oversize(self) -> None:
        limit = _overlay().direction.max_value_bytes
        value = (
            b'{"command_id":"'
            + COMMAND_ID.encode("ascii")
            + b'","padding":"'
            + b"x" * (limit + 1)
            + b'"}'
        )
        decision = _refuses(
            EnumEdgeRefusal.OVERSIZE,
            recording=(_event(value_b64=_b64(value)),),
        )
        assert decision.key_digest is not None

    def test_duplicate_member(self) -> None:
        _refuses(
            EnumEdgeRefusal.DUPLICATE_MEMBER,
            recording=(_event(value_b64=_b64(_duplicate_member_value())),),
        )

    def test_shape_violation(self) -> None:
        _refuses(
            EnumEdgeRefusal.SHAPE_VIOLATION,
            recording=(_event(value_b64=_b64(_marker_value())),),
        )

    def test_key_absent(self) -> None:
        decision = _refuses(
            EnumEdgeRefusal.KEY_ABSENT,
            recording=(_event(key_b64=None),),
        )
        assert decision.key_digest is None

    def test_key_shape_violation(self) -> None:
        _refuses(
            EnumEdgeRefusal.KEY_SHAPE_VIOLATION,
            recording=(_event(key_b64=_b64(b"not-a-uuid-key")),),
        )

    def test_key_binding_mismatch(self) -> None:
        # A UUID-shaped key that is not the value's command_id.
        decision = _refuses(
            EnumEdgeRefusal.KEY_BINDING_MISMATCH,
            recording=(_event(key_b64=_b64(OTHER_UUID.encode("ascii"))),),
        )
        assert decision.key_digest is not None

    def test_a_header_name_outside_the_allow_list_refuses(self) -> None:
        headers = (
            *_event().headers,
            ModelWireHeader(name="x-trace", value="synthetic"),
        )
        _refuses(
            EnumEdgeRefusal.HEADER_NOT_ALLOWED,
            recording=(_event(headers=headers),),
        )

    def test_a_header_value_off_its_rule_refuses(self) -> None:
        overlay = _overlay()
        _refuses(
            EnumEdgeRefusal.HEADER_NOT_ALLOWED,
            recording=(
                _event(
                    headers=(
                        ModelWireHeader(
                            name=overlay.direction.message_id_header,
                            value="not-the-message-id-pattern",
                        ),
                    )
                ),
            ),
        )

    def test_duplicate_header(self) -> None:
        headers = _event().headers + _event().headers
        _refuses(
            EnumEdgeRefusal.DUPLICATE_HEADER,
            recording=(_event(headers=headers),),
        )

    def test_no_message_id(self) -> None:
        _refuses(
            EnumEdgeRefusal.NO_MESSAGE_ID,
            recording=(_event(headers=()),),
        )

    def test_stamp_present(self) -> None:
        overlay = _overlay()
        headers = (
            *_event().headers,
            ModelWireHeader(
                name=overlay.direction.stamp_headers.origin_lane,
                value=overlay.binding.origin_lane,
            ),
        )
        _refuses(
            EnumEdgeRefusal.STAMP_PRESENT,
            recording=(_event(headers=headers),),
        )


class TestOrderOfChecks:
    def test_the_selection_count_beats_a_bad_key(self) -> None:
        recording = (_event(), _event(key_b64=_b64(b"not-a-uuid-key")))
        decision = _refuses(EnumEdgeRefusal.UNIT_NOT_SINGLE, recording=recording)
        assert decision.topic is None

    def test_the_identity_beats_a_stale_probe(self) -> None:
        max_age = _overlay().direction.to.acl_probe_max_age_seconds
        _refuses(
            EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH,
            authenticated_identity=OTHER_PRINCIPAL,
            acl_probe=_probe(recorded_at=NOW - timedelta(seconds=max_age + 60)),
        )

    def test_a_stale_probe_beats_a_bad_value(self) -> None:
        max_age = _overlay().direction.to.acl_probe_max_age_seconds
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_STALE,
            recording=(_event(value_b64=_b64(_marker_value())),),
            acl_probe=_probe(recorded_at=NOW - timedelta(seconds=max_age + 60)),
        )


class TestTheGateIsWhatRefuses:
    """Failing control: an always-pass ``gate_egress`` in the handler module
    lets the customer content through, so the refusals above are the gate's,
    not the decision's own copies of them."""

    def test_with_the_gate_replaced_the_customer_content_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra_member = _event(value_b64=_b64(_marker_value()))
        key_differs = _event(key_b64=_b64(OTHER_UUID.encode("ascii")))
        duplicate_member = _event(value_b64=_b64(_duplicate_member_value()))
        events = (extra_member, key_differs, duplicate_member)
        # With the real gate in place each refuses with its own gate reason.
        assert (
            _handler().handle(_request(recording=(extra_member,))).reason
            is EnumEdgeRefusal.SHAPE_VIOLATION
        )
        assert (
            _handler().handle(_request(recording=(key_differs,))).reason
            is EnumEdgeRefusal.KEY_BINDING_MISMATCH
        )
        assert (
            _handler().handle(_request(recording=(duplicate_member,))).reason
            is EnumEdgeRefusal.DUPLICATE_MEMBER
        )
        calls: list[ModelEgressGateRequest] = []

        def always_pass(request: ModelEgressGateRequest) -> ModelEgressGateResult:
            calls.append(request)
            return ModelEgressGateResult(
                outcome=EnumEgressOutcome.PASS,
                reason=None,
                key_digest="0" * 64,
                value_size=0,
                message_id=MESSAGE_ID,
            )

        monkeypatch.setattr(edge_module, "gate_egress", always_pass)
        for event in events:
            decision = _handler().handle(_request(recording=(event,)))
            assert decision.outcome is EnumEgressOutcome.PASS
            record = decision.record_out
            assert record is not None
            assert record.key_b64 == event.key_b64
            assert record.value_b64 == event.value_b64
        assert len(calls) == 3
        carried = _handler().handle(_request(recording=(extra_member,))).record_out
        assert carried is not None
        marker_bytes = base64.b64decode(carried.value_b64)
        assert marker_bytes == _marker_value()
        assert MARKER.encode("ascii") in marker_bytes


class TestTheRouteRefusalNeedsTheRealResolver:
    """Failing control: with ``resolve_physical_topic`` in the handler module
    replaced, a route-mutated request passes -- the route refusal is the real
    resolver's answer, not the request's other fields."""

    def test_a_fake_resolver_agreeing_lets_the_mutated_route_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = _request(
            overlay=_overlay_with(pinned_physical_topic=PINNED_ONE_CHARACTER_CHANGED)
        )
        assert _handler().handle(request).reason is EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT

        def agreeing(topic: str, *, tenant_slug: str) -> str:
            return PINNED_ONE_CHARACTER_CHANGED

        monkeypatch.setattr(edge_module, "resolve_physical_topic", agreeing)
        decision = _handler().handle(request)
        assert decision.outcome is EnumEgressOutcome.PASS
        record = decision.record_out
        assert record is not None
        assert record.topic == PINNED_ONE_CHARACTER_CHANGED

    def test_a_resolver_answering_another_tenants_topic_refuses_the_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The unmutated request passes with the real resolver; a resolver that
        # answers with the pinned topic of another tenant makes it refuse:
        # the route check reads the resolver's answer.
        assert _handler().handle(_request()).outcome is EnumEgressOutcome.PASS

        def another_tenants_pin(topic: str, *, tenant_slug: str) -> str:
            return OTHER_TENANT_PINNED

        monkeypatch.setattr(edge_module, "resolve_physical_topic", another_tenants_pin)
        decision = _handler().handle(_request())
        assert decision.outcome is EnumEgressOutcome.REFUSE
        assert decision.reason is EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT


class TestTheIdentityCheckReadsTheBinding:
    """Failing control: nothing is replaced -- the wire identity is compared
    with the binding's principal, never with the overlay's configured names."""

    def test_the_binding_identity_passes_and_another_name_refuses(self) -> None:
        overlay = _overlay()
        assert overlay.direction.to.auth_ref == overlay.binding.principal_ref
        assert (
            overlay.direction.acl.write_principal_ref == overlay.binding.principal_ref
        )
        passing = _handler().handle(
            _request(authenticated_identity=overlay.binding.principal_ref)
        )
        assert passing.outcome is EnumEgressOutcome.PASS
        refusing = _handler().handle(_request(authenticated_identity=OTHER_PRINCIPAL))
        assert refusing.reason is EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH

    def test_an_overlay_configured_name_is_never_the_compared_identity(
        self,
    ) -> None:
        configured = "example-edge-writer-configured"
        # model_copy bypasses the overlay validator, which would refuse at
        # load a configured name that disagrees with the binding's; the two
        # configured names stay equal to each other.
        overlay = _overlay()
        direction = overlay.direction.model_copy(
            update={
                "to": overlay.direction.to.model_copy(update={"auth_ref": configured}),
                "acl": overlay.direction.acl.model_copy(
                    update={"write_principal_ref": configured}
                ),
            }
        )
        mutated = overlay.model_copy(update={"direction": direction})
        assert mutated.direction.to.auth_ref == configured
        assert (
            mutated.direction.acl.write_principal_ref == mutated.direction.to.auth_ref
        )
        assert mutated.direction.to.auth_ref != mutated.binding.principal_ref
        # The wire identity equals the binding: the decision passes even
        # though the overlay names another principal. This is the test that
        # fails if the handler compared the overlay's configured name.
        passing = _handler().handle(
            _request(
                overlay=mutated,
                authenticated_identity=mutated.binding.principal_ref,
            )
        )
        assert passing.outcome is EnumEgressOutcome.PASS
        assert passing.record_out is not None
        # The overlay's configured name as the wire identity: refused.
        refusing = _handler().handle(
            _request(overlay=mutated, authenticated_identity=configured)
        )
        assert refusing.reason is EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH
