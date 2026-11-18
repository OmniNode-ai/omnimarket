# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFederationEdgeDecision: the pure decision of one edge step.

One in-memory runtime hands its recorded events to one lane through one
topic; ``handle(request: ModelEdgeDecisionRequest) -> ModelEdgeDecision``
selects the recording's event on the overlay's allowed topic and answers
one decision for it. Sync and pure: no I/O, no clock (``now`` arrives in
the request), no env, no envelope, and the key and the value leave this
function only as the base64 strings they came as, inside ``record_out`` —
the handler never re-encodes either.

The checks run in this order and the first failure returns the refusal,
with ``not_selected`` always set, ``topic`` the selected event's topic once
one event is selected (``None`` before that), and ``key_digest`` and
``message_id`` only once the gate read them:

1. SELECT: the recording's events on the overlay's allowed topic; exactly
   one event is decided, so zero or several is ``unit-not-single``.
2. TOPIC: the registry entry names the allowed topic
   (``topic-not-allowed``), and both the entry and the direction declare
   the topic class ``internal`` (``class-not-declared``).
3. WHO AND WHERE: the connection declared the destination lane and
   authenticated with the mechanism the lane declares
   (``lane-unauthenticated``); the wire-observed authenticated identity is
   the binding's principal — the overlay's configured names are never an
   input here (``principal-binding-mismatch``); the destination lane is
   permitted (``lane-not-permitted``); the pinned physical topic is the one
   runtime topic resolver's answer for the allowed topic under the
   binding's tenant slug, which the pinned wire topic itself names under
   the tenant-wire-prefix mapping (``route-outside-tenant``).
4. ACL PROBE: the probe was measured (``acl-not-measured``) and the
   request's ``now`` is present (``now-absent``); the probe is this
   direction's, on this lane, for this binding's principal, with exactly
   one step of each letter a..e (``acl-probe-mismatch``); it is fresh
   against ``now``, the max age compared exactly (``acl-probe-stale``);
   every step expected ``denied`` was observed ``denied``
   (``acl-probe-not-denied``) and every step expected ``allowed`` was
   observed ``allowed`` (``acl-probe-mismatch``).
5. GATE: the record is gated by the egress gate under the policy built from
   the overlay's wire limits and the registry entry's closed value shape; a
   gate refusal carries the gate's own reason name, word for word.
6. PASS: the record as it may leave the edge — topic the pinned physical
   topic, key and value base64 strings unchanged, headers followed by the
   three stamp headers (origin lane, source lane, first hop).
"""

from __future__ import annotations

from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_physical_topic,
    resolve_tenant_from_wire_topic,
)

from omnimarket.models.federation_egress.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressGateRequest,
    ModelEgressTopicPolicy,
    ModelWireHeader,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumEdgeRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelEdgeDecision,
    ModelEdgeDecisionRequest,
    ModelRecordOut,
)
from omnimarket.nodes.node_federation_egress_gate_compute.handlers.handler_federation_egress_gate import (
    gate_egress,
)

# The hops stamp of a record an edge carries: slice 1 is the first and only
# hop a runtime record has taken when it leaves this edge.
FIRST_HOP = "1"

# The five ACL probe letters, each exactly once, in the probe's own order.
_PROBE_STEP_LETTERS = ("a", "b", "c", "d", "e")


def _is_blank(text: str) -> bool:
    """Whether a mechanism name is blank: empty or whitespace only."""
    return not text.strip()


def _refuse(
    reason: EnumEdgeRefusal,
    *,
    not_selected: int,
    topic: str | None,
    key_digest: str | None = None,
    message_id: str | None = None,
) -> ModelEdgeDecision:
    """One refusal: the named reason, never a record, and the identifying
    fields only where they were already known."""
    return ModelEdgeDecision(
        outcome=EnumEgressOutcome.REFUSE,
        reason=reason,
        record_out=None,
        not_selected=not_selected,
        topic=topic,
        key_digest=key_digest,
        message_id=message_id,
        terminal_failure_cause=reason,
    )


class HandlerFederationEdgeDecision:
    """The federation edge decision: pure compute, one recording in, one answer out."""

    def handle(self, request: ModelEdgeDecisionRequest) -> ModelEdgeDecision:
        """Decide the recording's one event on the overlay's allowed topic; the
        same request gives the same answer, and the key and the value leave
        this function only as the base64 strings they came as, inside
        ``record_out``."""
        overlay = request.overlay
        direction = overlay.direction
        binding = overlay.binding

        # 1. SELECT: the recording's events on the allowed topic. Exactly one
        # event is decided; nothing else in the recording is read further.
        selected = [
            recorded
            for recorded in request.recording
            if recorded.topic == direction.allowed_topic
        ]
        not_selected = len(request.recording) - len(selected)
        if len(selected) != 1:
            return _refuse(
                EnumEdgeRefusal.UNIT_NOT_SINGLE, not_selected=not_selected, topic=None
            )
        event = selected[0]

        # 2. TOPIC: the registry entry names the allowed topic, and both the
        # entry and the direction declare the topic class internal.
        registry_entry = request.registry_entry
        if (
            registry_entry is not None
            and registry_entry.topic != direction.allowed_topic
        ):
            return _refuse(
                EnumEdgeRefusal.TOPIC_NOT_ALLOWED,
                not_selected=not_selected,
                topic=event.topic,
            )
        if (
            registry_entry is None
            or "internal" not in registry_entry.classes
            or "internal" not in direction.classes
        ):
            return _refuse(
                EnumEdgeRefusal.CLASS_NOT_DECLARED,
                not_selected=not_selected,
                topic=event.topic,
            )

        # 3. WHO AND WHERE: the connection declared the destination lane and
        # authenticated with the mechanism the lane declares.
        lane_declaration = request.lane_declaration
        if (
            lane_declaration is None
            or lane_declaration.lane != direction.to.lane
            or lane_declaration.auth_mechanism is None
            or _is_blank(lane_declaration.auth_mechanism)
            or request.connection_mechanism is None
            or request.connection_mechanism != lane_declaration.auth_mechanism
        ):
            return _refuse(
                EnumEdgeRefusal.LANE_UNAUTHENTICATED,
                not_selected=not_selected,
                topic=event.topic,
            )
        # The wire-observed identity is compared with the binding's principal:
        # an overlay-configured name is never an input to this check.
        if (
            request.authenticated_identity is None
            or request.authenticated_identity != binding.principal_ref
        ):
            return _refuse(
                EnumEdgeRefusal.PRINCIPAL_BINDING_MISMATCH,
                not_selected=not_selected,
                topic=event.topic,
            )
        if direction.to.lane not in binding.permitted_lanes:
            return _refuse(
                EnumEdgeRefusal.LANE_NOT_PERMITTED,
                not_selected=not_selected,
                topic=event.topic,
            )

        # ROUTE: the pinned physical topic is the one runtime topic resolver's
        # answer -- for the allowed topic under the binding's tenant slug, and
        # (under the tenant-wire-prefix mapping) the pinned wire topic itself
        # must name that slug. A resolver refusal refuses the route.
        try:
            if direction.wire_topic_mapping == "tenant_wire_prefix":
                resolved = resolve_physical_topic(
                    direction.allowed_topic, tenant_slug=binding.tenant_slug_ref
                )
                if resolved != direction.pinned_physical_topic:
                    return _refuse(
                        EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
                        not_selected=not_selected,
                        topic=event.topic,
                    )
                slug, _canonical_topic = resolve_tenant_from_wire_topic(
                    direction.pinned_physical_topic
                )
                if slug != binding.tenant_slug_ref:
                    return _refuse(
                        EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
                        not_selected=not_selected,
                        topic=event.topic,
                    )
            elif direction.allowed_topic != direction.pinned_physical_topic:
                return _refuse(
                    EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
                    not_selected=not_selected,
                    topic=event.topic,
                )
        except ValueError:
            return _refuse(
                EnumEdgeRefusal.ROUTE_OUTSIDE_TENANT,
                not_selected=not_selected,
                topic=event.topic,
            )

        # 4. ACL PROBE: the probe was measured, and the decision's own now is
        # present to measure its freshness against.
        probe = request.acl_probe
        if probe is None:
            return _refuse(
                EnumEdgeRefusal.ACL_NOT_MEASURED,
                not_selected=not_selected,
                topic=event.topic,
            )
        now = request.now
        if now is None:
            return _refuse(
                EnumEdgeRefusal.NOW_ABSENT,
                not_selected=not_selected,
                topic=event.topic,
            )
        if (
            probe.probe_ref != direction.to.acl_probe_ref
            or probe.lane != direction.to.lane
            or probe.principal_ref != binding.principal_ref
            or tuple(sorted(step.step for step in probe.steps)) != _PROBE_STEP_LETTERS
        ):
            return _refuse(
                EnumEdgeRefusal.ACL_PROBE_MISMATCH,
                not_selected=not_selected,
                topic=event.topic,
            )
        # Both stamps are timezone-aware (refused at the model boundary
        # otherwise), so the comparison and the age are well defined. The max
        # age passes exactly.
        if (
            probe.recorded_at > now
            or (now - probe.recorded_at).total_seconds()
            > direction.to.acl_probe_max_age_seconds
        ):
            return _refuse(
                EnumEdgeRefusal.ACL_PROBE_STALE,
                not_selected=not_selected,
                topic=event.topic,
            )
        if any(
            step.expected == "denied" and step.observed == "allowed"
            for step in probe.steps
        ):
            return _refuse(
                EnumEdgeRefusal.ACL_PROBE_NOT_DENIED,
                not_selected=not_selected,
                topic=event.topic,
            )
        if any(
            step.expected == "allowed" and step.observed == "denied"
            for step in probe.steps
        ):
            return _refuse(
                EnumEdgeRefusal.ACL_PROBE_MISMATCH,
                not_selected=not_selected,
                topic=event.topic,
            )

        # 5. GATE: the record is gated under the policy built from the
        # overlay's wire limits and the registry entry's closed value shape.
        stamp_headers = direction.stamp_headers
        policy = ModelEgressTopicPolicy(
            fields=registry_entry.fields,
            key_pattern=direction.key_pattern,
            key_binding=direction.key_binding,
            header_allow_list=direction.header_allow_list,
            max_headers=direction.max_headers,
            max_value_bytes=direction.max_value_bytes,
            max_value_depth=direction.max_value_depth,
            stamp_header_names=(
                stamp_headers.origin_lane,
                stamp_headers.source_lane,
                stamp_headers.hops,
            ),
            message_id_header=direction.message_id_header,
        )
        result = gate_egress(
            ModelEgressGateRequest(
                topic=event.topic,
                key_b64=event.key_b64,
                value_b64=event.value_b64,
                headers=event.headers,
                policy=policy,
            )
        )
        if result.outcome is EnumEgressOutcome.REFUSE:
            reason = result.reason
            # The gate result model guarantees a refuse result names one
            # reason, and EnumEdgeRefusal lists every gate refusal name.
            assert reason is not None
            return _refuse(
                EnumEdgeRefusal(reason.value),
                not_selected=not_selected,
                topic=event.topic,
                key_digest=result.key_digest,
                message_id=result.message_id,
            )

        # 6. PASS: the record as it may leave the edge. The gate refused
        # every record with no key (key-absent), so a passed record always
        # carried one; both strings go out exactly as they came in.
        assert event.key_b64 is not None
        record_out = ModelRecordOut(
            topic=direction.pinned_physical_topic,
            key_b64=event.key_b64,
            value_b64=event.value_b64,
            headers=(
                *event.headers,
                ModelWireHeader(
                    name=stamp_headers.origin_lane, value=binding.origin_lane
                ),
                ModelWireHeader(
                    name=stamp_headers.source_lane, value=binding.origin_lane
                ),
                ModelWireHeader(name=stamp_headers.hops, value=FIRST_HOP),
            ),
        )
        return ModelEdgeDecision(
            outcome=EnumEgressOutcome.PASS,
            reason=None,
            record_out=record_out,
            not_selected=not_selected,
            topic=record_out.topic,
            key_digest=result.key_digest,
            message_id=result.message_id,
        )


def decide_edge(request: ModelEdgeDecisionRequest) -> ModelEdgeDecision:
    """One edge decision: the handler's pure answer for one recording."""
    return HandlerFederationEdgeDecision().handle(request)


__all__: list[str] = [
    "FIRST_HOP",
    "HandlerFederationEdgeDecision",
    "decide_edge",
]
