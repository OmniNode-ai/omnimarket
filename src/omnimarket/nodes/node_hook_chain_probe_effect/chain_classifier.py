# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure per-leg classification for the hook->cloud chain probe (OMN-17202).

ZERO I/O. Takes the five structural observations the effect boundary collected
and returns the typed union verdict: the furthest leg reached, the leg it died
at, and the blockers that explain it.

Two cloud-leg distinctions matter as much as leg 3 does, and for the same
reason. A 401 from onex-api means "unmatched path" just as readily as it means
"refused", so a route that was never deployed is only distinguishable from a
credential problem by reading the gateway's own public route list -- an absent
route reports GATEWAY_ROUTE_ABSENT, and an UNKNOWN route list never fabricates
one. And the leg-5 supplier answers 200 for "found", "not_found" and
"projection_absent" alike, so a sink that does not exist yet (OMN-17201) is
reported as PROJECTION_SINK_ABSENT rather than as a row that failed to arrive.

Leg 3 is the whole point. Three independent structural facts can each stop the
relay, and they were all true simultaneously on 2026-08-30:

  * the hook topic is absent from the forwarder's contract-declared outbound
    mirror set  -> ALLOWLIST_DENIED (OMN-16979);
  * the forwarder is not attached to the lane the hooks publish to, evidenced
    by its own liveness topic carrying nothing there -> LANE_MISMATCH
    (OMN-17034);
  * the cloud leg is still direct-MSK rather than the HTTPS relay
    -> TRANSPORT_NOT_RELAY (OMN-16459).

All three are reported; the first in that order is the primary. Only when NONE
of them holds does an absent consumer advance classify as NO_CONSUMER. That
ordering is what makes "denied" distinguishable from "nothing is listening" --
the distinction the ticket names as its own acceptance bar.
"""

from __future__ import annotations

from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    LEG_ORDER,
    EnumHookChainBlocker,
    EnumHookChainLeg,
    ModelCloudGatewayObservation,
    ModelCloudProjectionObservation,
    ModelForwarderObservation,
    ModelHookChainLegResult,
    ModelHookChainProbeResult,
    ModelLocalBusObservation,
    ModelLocalEmitObservation,
)

#: The canonical cloud-leg transport. Anything else is the pre-OMN-16459 shape.
_RELAY_TRANSPORT = "https_relay"

#: HTTP statuses that mean "the read route refused us", not "the row is absent".
_UNAUTHORIZED_STATUSES = frozenset({401, 403})

#: The supplier route (OMN-17205) answers HTTP 200 for all three data states, so
#: the sink-does-not-exist case is only visible in the body. Folding it into
#: "row absent" would point the operator at the relay when the defect is a table
#: that was never created -- the OMN-15797 silent-blinding class.
_PROJECTION_ABSENT_STATE = "projection_absent"


def _classify_local_emit(
    emit: ModelLocalEmitObservation | None,
) -> ModelHookChainLegResult:
    if emit is None:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.LOCAL_EMIT,
            reached=False,
            blocker=EnumHookChainBlocker.NOT_ATTEMPTED,
            evidence="leg not probed",
        )
    if emit.emitted:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.LOCAL_EMIT,
            reached=True,
            blocker=EnumHookChainBlocker.NONE,
            evidence=f"emitted {emit.topic} on lane {emit.lane}",
        )
    if not emit.lane_resolved:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.LOCAL_EMIT,
            reached=False,
            blocker=EnumHookChainBlocker.HOOK_LANE_UNRESOLVED,
            evidence=emit.detail
            or "no hook env authority declares the hook edge lane; refused to "
            "fall back to the ambient shell",
        )
    return ModelHookChainLegResult(
        leg=EnumHookChainLeg.LOCAL_EMIT,
        reached=False,
        blocker=EnumHookChainBlocker.EMIT_REFUSED,
        evidence=emit.detail or f"emit refused for {emit.topic} on lane {emit.lane}",
    )


def _classify_local_bus(
    bus: ModelLocalBusObservation | None,
) -> ModelHookChainLegResult:
    if bus is None:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.LOCAL_BUS,
            reached=False,
            blocker=EnumHookChainBlocker.NOT_ATTEMPTED,
            evidence="leg not probed",
        )
    if bus.observed:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.LOCAL_BUS,
            reached=True,
            blocker=EnumHookChainBlocker.NONE,
            evidence=f"readback at offset {bus.offset} on lane {bus.lane}",
        )
    return ModelHookChainLegResult(
        leg=EnumHookChainLeg.LOCAL_BUS,
        reached=False,
        blocker=EnumHookChainBlocker.NOT_ON_BUS,
        evidence=bus.detail or f"no correlated record on {bus.topic} (lane {bus.lane})",
    )


def _forwarder_structural_blockers(
    *, hook_topic: str, emit_lane: str, forwarder: ModelForwarderObservation
) -> tuple[tuple[EnumHookChainBlocker, str], ...]:
    """Policy and attachment facts that stop the relay regardless of any deadline."""
    found: list[tuple[EnumHookChainBlocker, str]] = []
    if hook_topic not in forwarder.mirror_outbound_topics:
        found.append(
            (
                EnumHookChainBlocker.ALLOWLIST_DENIED,
                f"{hook_topic} absent from the forwarder outbound mirror set "
                f"({len(forwarder.mirror_outbound_topics)} topics declared)",
            )
        )
    if not forwarder.forwarder_present_on_emit_lane:
        found.append(
            (
                EnumHookChainBlocker.LANE_MISMATCH,
                f"forwarder liveness topic {forwarder.liveness_topic} carries no "
                f"records on lane {emit_lane}, the lane the hooks publish to -- "
                "the forwarder is attached elsewhere",
            )
        )
    if forwarder.cloud_leg_transport != _RELAY_TRANSPORT:
        found.append(
            (
                EnumHookChainBlocker.TRANSPORT_NOT_RELAY,
                f"cloud leg transport is {forwarder.cloud_leg_transport}, "
                f"not {_RELAY_TRANSPORT}",
            )
        )
    return tuple(found)


def _classify_forwarder(
    *,
    hook_topic: str,
    emit_lane: str,
    forwarder: ModelForwarderObservation | None,
) -> tuple[ModelHookChainLegResult, tuple[EnumHookChainBlocker, ...]]:
    if forwarder is None:
        return (
            ModelHookChainLegResult(
                leg=EnumHookChainLeg.FORWARDER_RELAY,
                reached=False,
                blocker=EnumHookChainBlocker.NOT_ATTEMPTED,
                evidence="leg not probed",
            ),
            (),
        )

    structural = _forwarder_structural_blockers(
        hook_topic=hook_topic, emit_lane=emit_lane, forwarder=forwarder
    )
    if structural:
        primary, _evidence = structural[0]
        return (
            ModelHookChainLegResult(
                leg=EnumHookChainLeg.FORWARDER_RELAY,
                reached=False,
                blocker=primary,
                evidence="; ".join(text for _, text in structural),
            ),
            tuple(blocker for blocker, _ in structural),
        )

    if not forwarder.consumer_group_advanced:
        # Policy admits the topic, the forwarder IS attached to this lane, and
        # the transport is the relay -- and the group still did not advance:
        # nothing is consuming. Distinct from a denial and from a wrong lane,
        # and the reason all three are separately named.
        return (
            ModelHookChainLegResult(
                leg=EnumHookChainLeg.FORWARDER_RELAY,
                reached=False,
                blocker=EnumHookChainBlocker.NO_CONSUMER,
                evidence=forwarder.detail
                or "topic admitted, forwarder attached to this lane, transport "
                "correct, consumer group did not advance",
            ),
            (EnumHookChainBlocker.NO_CONSUMER,),
        )

    return (
        ModelHookChainLegResult(
            leg=EnumHookChainLeg.FORWARDER_RELAY,
            reached=True,
            blocker=EnumHookChainBlocker.NONE,
            evidence=f"mirrored on lane {emit_lane} over {forwarder.cloud_leg_transport}",
        ),
        (),
    )


def _classify_cloud_gateway(
    gateway: ModelCloudGatewayObservation | None,
) -> ModelHookChainLegResult:
    if gateway is None:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_GATEWAY,
            reached=False,
            blocker=EnumHookChainBlocker.NOT_ATTEMPTED,
            evidence="leg not probed",
        )
    if not gateway.reachable:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_GATEWAY,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_UNREACHABLE,
            evidence=gateway.detail or "cloud gateway read route unreachable",
        )
    if gateway.route_served is False:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_GATEWAY,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_ROUTE_ABSENT,
            evidence=gateway.detail
            or "the declared cloud ingest path is absent from the gateway's own "
            "public route list -- never deployed, not refused",
        )
    if gateway.status_code in _UNAUTHORIZED_STATUSES:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_GATEWAY,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_UNAUTHORIZED,
            evidence=f"cloud gateway read route returned {gateway.status_code} "
            "-- refused, not empty",
        )
    if not gateway.correlation_found:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_GATEWAY,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_INGEST_ABSENT,
            evidence=gateway.detail
            or "cloud gateway readable but the correlation id was not ingested",
        )
    return ModelHookChainLegResult(
        leg=EnumHookChainLeg.CLOUD_GATEWAY,
        reached=True,
        blocker=EnumHookChainBlocker.NONE,
        evidence="correlation id observed at cloud gateway ingest",
    )


def _classify_cloud_projection(
    projection: ModelCloudProjectionObservation | None,
) -> ModelHookChainLegResult:
    if projection is None:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.NOT_ATTEMPTED,
            evidence="leg not probed",
        )
    if not projection.reachable:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_UNREACHABLE,
            evidence=projection.detail or "cloud projection read route unreachable",
        )
    if projection.route_served is False:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_ROUTE_ABSENT,
            evidence=projection.detail
            or "the declared cloud projection path is absent from the gateway's "
            "own public route list -- never deployed, not refused",
        )
    if projection.status_code in _UNAUTHORIZED_STATUSES:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.GATEWAY_UNAUTHORIZED,
            evidence=f"cloud projection read route returned {projection.status_code} "
            "-- refused, not empty",
        )
    if projection.data_state == _PROJECTION_ABSENT_STATE:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.PROJECTION_SINK_ABSENT,
            evidence=projection.detail
            or "the cloud hook projection itself does not exist on this plane "
            "(supplier route answered data_state=projection_absent) -- the sink "
            "was never built, so no row could have landed in it",
        )
    if not projection.row_found:
        return ModelHookChainLegResult(
            leg=EnumHookChainLeg.CLOUD_PROJECTION,
            reached=False,
            blocker=EnumHookChainBlocker.PROJECTION_ROW_ABSENT,
            evidence=projection.detail
            or "cloud projection readable and the correlated row is absent",
        )
    return ModelHookChainLegResult(
        leg=EnumHookChainLeg.CLOUD_PROJECTION,
        reached=True,
        blocker=EnumHookChainBlocker.NONE,
        evidence="correlated row readable in the cloud projection",
    )


def classify_chain(
    *,
    correlation_id: str,
    hook_topic: str,
    emit: ModelLocalEmitObservation | None,
    bus: ModelLocalBusObservation | None,
    forwarder: ModelForwarderObservation | None,
    gateway: ModelCloudGatewayObservation | None,
    projection: ModelCloudProjectionObservation | None,
) -> ModelHookChainProbeResult:
    """Classify five structural observations into the union verdict."""
    emit_lane = emit.lane if emit is not None else ""

    forwarder_result, forwarder_blockers = _classify_forwarder(
        hook_topic=hook_topic, emit_lane=emit_lane, forwarder=forwarder
    )
    by_leg: dict[EnumHookChainLeg, ModelHookChainLegResult] = {
        EnumHookChainLeg.LOCAL_EMIT: _classify_local_emit(emit),
        EnumHookChainLeg.LOCAL_BUS: _classify_local_bus(bus),
        EnumHookChainLeg.FORWARDER_RELAY: forwarder_result,
        EnumHookChainLeg.CLOUD_GATEWAY: _classify_cloud_gateway(gateway),
        EnumHookChainLeg.CLOUD_PROJECTION: _classify_cloud_projection(projection),
    }

    legs = tuple(by_leg[leg] for leg in LEG_ORDER)

    furthest: EnumHookChainLeg | None = None
    failed: EnumHookChainLeg | None = None
    for leg_result in legs:
        if leg_result.reached:
            furthest = leg_result.leg
            continue
        failed = leg_result.leg
        break

    if failed is None:
        return ModelHookChainProbeResult(
            correlation_id=correlation_id,
            hook_topic=hook_topic,
            legs=legs,
            furthest_leg_reached=furthest,
            failed_leg=None,
            primary_blocker=EnumHookChainBlocker.NONE,
            blockers=(),
            chain_complete=True,
        )

    failing = by_leg[failed]
    blockers = (
        forwarder_blockers
        if failed is EnumHookChainLeg.FORWARDER_RELAY and forwarder_blockers
        else (failing.blocker,)
    )
    return ModelHookChainProbeResult(
        correlation_id=correlation_id,
        hook_topic=hook_topic,
        legs=legs,
        furthest_leg_reached=furthest,
        failed_leg=failed,
        primary_blocker=failing.blocker,
        blockers=blockers,
        chain_complete=False,
    )


__all__: list[str] = ["classify_chain"]
