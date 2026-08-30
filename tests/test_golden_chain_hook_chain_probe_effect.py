# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for node_hook_chain_probe_effect (OMN-17202).

The gap this node closes: every ticket on the hook->cloud chain proves its own
leg and the union is never tested. These tests pin the two properties that make
the union testable:

  * AC1 - one invocation returns a typed per-leg result naming the furthest leg
    reached, for a live chain and for a dead one.
  * AC2 - against the chain's shape as measured 2026-08-30 the probe reports
    failure at leg 3 naming the ALLOWLIST denial and the LANE mismatch, and
    NEVER a generic timeout. A probe that cannot tell "denied by allowlist"
    apart from "no consumer" does not satisfy the ticket, so both shapes are
    asserted to produce distinct blockers.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_hook_chain_probe_effect.chain_classifier import (
    classify_chain,
)
from omnimarket.nodes.node_hook_chain_probe_effect.handlers.handler_hook_chain_probe import (
    HandlerHookChainProbe,
)
from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    EnumHookChainBlocker,
    EnumHookChainLeg,
    ModelCloudGatewayObservation,
    ModelCloudProjectionObservation,
    ModelForwarderObservation,
    ModelHookChainAddress,
    ModelHookChainProbeRequest,
    ModelLocalBusObservation,
    ModelLocalEmitObservation,
)

HOOK_TOPIC = "onex.evt.omniclaude.tool-executed.v1"
STABILITY_LANE = "stability-test"
DEV_LANE = "dev"

# The forwarder's contract-declared outbound mirror set as it stands on dev
# (omnibase_infra node_bus_forwarder_effect contract, OMN-16204): the bare
# session-lifecycle pair is admitted, every other omniclaude hook class is not.
OUTBOUND_ALLOWLIST = (
    "onex.evt.omnibase-infra.inference-response.v1",
    "onex.evt.omnibase-infra.delegation-completed.v1",
    "onex.evt.omnibase-infra.delegation-failed.v1",
    "onex.evt.omniintelligence.llm-call-completed.v1",
    "onex.evt.omnibase-infra.llm-call-completed.v1",
    "onex.evt.omnibase-infra.gateway-heartbeat.v1",
    "onex.evt.omniclaude.session-started.v1",
    "onex.evt.omniclaude.session-ended.v1",
)


def _emit(
    *, lane: str = STABILITY_LANE, emitted: bool = True
) -> ModelLocalEmitObservation:
    return ModelLocalEmitObservation(
        emitted=emitted, lane=lane, topic=HOOK_TOPIC, detail=None
    )


def _bus(
    *, observed: bool = True, lane: str = STABILITY_LANE
) -> ModelLocalBusObservation:
    return ModelLocalBusObservation(
        observed=observed,
        lane=lane,
        topic=HOOK_TOPIC,
        offset=61_600 if observed else None,
    )


#: The forwarder's own contract-declared canary topic; its presence on a lane is
#: the evidence of which lane the forwarder is attached to.
LIVENESS_TOPIC = "onex.evt.omnibase-infra.gateway-canary.v1"


def _forwarder(
    *,
    allowlist: tuple[str, ...] = OUTBOUND_ALLOWLIST,
    present_on_emit_lane: bool = False,
    transport: str = "kafka",
    advanced: bool = False,
) -> ModelForwarderObservation:
    """The chain's shape as measured 2026-08-30 unless a field is overridden.

    ``present_on_emit_lane`` defaults False because that is the live fact: the
    forwarder is bound to the dev lane while the hooks publish to stability, so
    its canary topic carries nothing where the hooks are.
    """
    return ModelForwarderObservation(
        mirror_outbound_topics=allowlist,
        cloud_leg_transport=transport,
        liveness_topic=LIVENESS_TOPIC,
        forwarder_present_on_emit_lane=present_on_emit_lane,
        consumer_group_advanced=advanced,
    )


class TestClassifierLegThreeIsDiagnostic:
    """AC2: leg 3 must name WHY, and the reasons must be distinguishable."""

    def test_live_chain_shape_reports_leg_three_allowlist_denied(self) -> None:
        result = classify_chain(
            correlation_id="cid-1",
            hook_topic=HOOK_TOPIC,
            emit=_emit(),
            bus=_bus(),
            forwarder=_forwarder(),
            gateway=None,
            projection=None,
        )
        assert result.failed_leg is EnumHookChainLeg.FORWARDER_RELAY
        assert result.furthest_leg_reached is EnumHookChainLeg.LOCAL_BUS
        assert result.primary_blocker is EnumHookChainBlocker.ALLOWLIST_DENIED
        assert EnumHookChainBlocker.TIMEOUT not in result.blockers
        assert result.chain_complete is False

    def test_live_chain_shape_also_reports_the_lane_mismatch(self) -> None:
        result = classify_chain(
            correlation_id="cid-2",
            hook_topic=HOOK_TOPIC,
            emit=_emit(),
            bus=_bus(),
            forwarder=_forwarder(),
            gateway=None,
            projection=None,
        )
        assert EnumHookChainBlocker.LANE_MISMATCH in result.blockers
        assert EnumHookChainBlocker.TRANSPORT_NOT_RELAY in result.blockers

    def test_allowlist_denial_is_distinct_from_no_consumer(self) -> None:
        admitted = classify_chain(
            correlation_id="cid-3",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay",
                present_on_emit_lane=True,
                advanced=False,
            ),
            gateway=None,
            projection=None,
        )
        assert admitted.failed_leg is EnumHookChainLeg.FORWARDER_RELAY
        assert admitted.primary_blocker is EnumHookChainBlocker.NO_CONSUMER
        assert EnumHookChainBlocker.ALLOWLIST_DENIED not in admitted.blockers

    def test_lane_mismatch_alone_is_not_reported_as_allowlist_denial(self) -> None:
        result = classify_chain(
            correlation_id="cid-4",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=STABILITY_LANE),
            bus=_bus(lane=STABILITY_LANE),
            forwarder=_forwarder(transport="https_relay", advanced=True),
            gateway=None,
            projection=None,
        )
        assert result.primary_blocker is EnumHookChainBlocker.LANE_MISMATCH
        assert EnumHookChainBlocker.ALLOWLIST_DENIED not in result.blockers


class TestClassifierUnionAndDegradation:
    def test_upstream_failure_marks_downstream_legs_not_attempted(self) -> None:
        result = classify_chain(
            correlation_id="cid-5",
            hook_topic=HOOK_TOPIC,
            emit=_emit(),
            bus=_bus(observed=False),
            forwarder=None,
            gateway=None,
            projection=None,
        )
        assert result.failed_leg is EnumHookChainLeg.LOCAL_BUS
        by_leg = {leg.leg: leg for leg in result.legs}
        assert by_leg[EnumHookChainLeg.FORWARDER_RELAY].blocker is (
            EnumHookChainBlocker.NOT_ATTEMPTED
        )
        assert by_leg[EnumHookChainLeg.CLOUD_PROJECTION].blocker is (
            EnumHookChainBlocker.NOT_ATTEMPTED
        )

    def test_unauthorized_cloud_read_is_not_a_missing_row(self) -> None:
        result = classify_chain(
            correlation_id="cid-6",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True, status_code=401, correlation_found=False
            ),
            projection=None,
        )
        assert result.failed_leg is EnumHookChainLeg.CLOUD_GATEWAY
        assert result.primary_blocker is EnumHookChainBlocker.GATEWAY_UNAUTHORIZED

    def test_full_green_chain_completes_at_cloud_projection(self) -> None:
        result = classify_chain(
            correlation_id="cid-7",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True, status_code=200, correlation_found=True
            ),
            projection=ModelCloudProjectionObservation(
                reachable=True, status_code=200, row_found=True
            ),
        )
        assert result.chain_complete is True
        assert result.furthest_leg_reached is EnumHookChainLeg.CLOUD_PROJECTION
        assert result.failed_leg is None
        assert result.primary_blocker is EnumHookChainBlocker.NONE
        assert len(result.legs) == 5


class _StubProbes:
    """One injected boundary standing in for all five legs' live I/O."""

    def __init__(
        self, *, address: ModelHookChainAddress, forwarder: ModelForwarderObservation
    ):
        self._address = address
        self._forwarder = forwarder
        self.emitted_correlation_ids: list[str] = []

    async def resolve_address(self) -> ModelHookChainAddress:
        return self._address

    async def emit(self, *, correlation_id: str, address: ModelHookChainAddress):
        self.emitted_correlation_ids.append(correlation_id)
        return ModelLocalEmitObservation(
            emitted=True, lane=address.emit_lane, topic=address.hook_topic, detail=None
        )

    async def read_local_bus(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ):
        return ModelLocalBusObservation(
            observed=True, lane=address.emit_lane, topic=address.hook_topic, offset=1
        )

    async def read_forwarder(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ):
        return self._forwarder

    async def read_cloud_gateway(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ):
        raise AssertionError("leg 4 must not be probed after leg 3 fails")

    async def read_cloud_projection(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ):
        raise AssertionError("leg 5 must not be probed after leg 3 fails")


@pytest.mark.asyncio
class TestHandlerSingleInvocation:
    async def test_one_invocation_mints_a_correlation_id_and_names_the_dead_leg(
        self,
    ) -> None:
        address = ModelHookChainAddress(
            hook_topic=HOOK_TOPIC,
            emit_lane=STABILITY_LANE,
            emit_lane_authority="<test fixture>",
            cloud_gateway_base_url="https://dev.api.omninode.ai",
        )
        probes = _StubProbes(address=address, forwarder=_forwarder())
        handler = HandlerHookChainProbe(probes=probes)

        result = await handler.handle(ModelHookChainProbeRequest())

        assert result.correlation_id
        assert probes.emitted_correlation_ids == [result.correlation_id]
        assert result.failed_leg is EnumHookChainLeg.FORWARDER_RELAY
        assert result.primary_blocker is EnumHookChainBlocker.ALLOWLIST_DENIED
        assert result.hook_topic == HOOK_TOPIC

    async def test_caller_supplied_correlation_id_is_carried_through(self) -> None:
        address = ModelHookChainAddress(
            hook_topic=HOOK_TOPIC,
            emit_lane=STABILITY_LANE,
            emit_lane_authority="<test fixture>",
            cloud_gateway_base_url="https://dev.api.omninode.ai",
        )
        probes = _StubProbes(address=address, forwarder=_forwarder())
        handler = HandlerHookChainProbe(probes=probes)

        result = await handler.handle(
            ModelHookChainProbeRequest(correlation_id="omn-17202-fixed")
        )

        assert result.correlation_id == "omn-17202-fixed"
        assert probes.emitted_correlation_ids == ["omn-17202-fixed"]


class TestLiveForwarderPolicyIsReadFromTheForwardersOwnContract:
    """The allowlist must come from omnibase_infra, never a copy kept here.

    A second copy of the mirror set in this repo would silently become a wrong
    allowlist the moment OMN-16979 widens the real one -- and the probe would
    then report a denial that no longer exists, or miss one that does.
    """

    def test_outbound_allowlist_is_read_from_the_installed_infra_contract(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            _load_forwarder_policy,
        )

        outbound, transport, detail = _load_forwarder_policy(
            "omnibase_infra.nodes.node_bus_forwarder_effect"
        )
        assert detail is None, detail
        assert outbound, "forwarder contract declares an outbound mirror set"
        assert transport

    def test_the_traced_hook_topic_is_denied_by_the_live_allowlist_today(self) -> None:
        """AC2, pinned against the real contract rather than a fixture."""
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            _load_forwarder_policy,
            _load_probe_config,
        )

        outbound, _transport, _detail = _load_forwarder_policy(
            "omnibase_infra.nodes.node_bus_forwarder_effect"
        )
        traced_topic = _load_probe_config()["hook_topic"]
        assert traced_topic not in outbound, (
            "the traced hook topic is now admitted by the forwarder outbound "
            "mirror set -- OMN-16979 has landed and this probe's AC2 expectation "
            "must be re-derived from the live chain, not left asserting a denial "
            "that no longer exists"
        )


class TestHookEdgeLaneIsResolvedFromTheHooksOwnAuthority:
    """OMN-17010 regression: a verifier must never inherit the ambient lane.

    ``ModelKafkaEventBusConfig.default()`` applies environment overrides, so a
    probe built on it emits to whatever ``KAFKA_BOOTSTRAP_SERVERS`` the calling
    shell happens to carry. That is not a hypothetical: on this Mac the hooks'
    own authority (``~/.omnibase/.env``, sourced by ``common.sh``) declares the
    stability lane while the ambient interactive shell carries the dev lane, and
    that exact ambiguity produced the wrong-broker verdict that flipped
    OMN-16162 out of Done and produced the falsified OMN-16996.

    A probe that emits to a lane the hooks do not publish to proves nothing
    about the hook chain, so the lane MUST come from the hooks' own env
    authority in the hooks' own precedence order, and an unresolvable lane MUST
    refuse rather than guess.
    """

    def test_lane_comes_from_the_hook_env_authority_not_the_ambient_shell(
        self, tmp_path, monkeypatch
    ) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            resolve_hook_edge_lane,
        )

        monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "ambient-lane:19092")
        authority = tmp_path / "omnibase.env"
        authority.write_text('KAFKA_BOOTSTRAP_SERVERS="hook-edge-lane:39092"\n')

        lane, source = resolve_hook_edge_lane([authority])

        assert lane == "hook-edge-lane:39092"
        assert str(authority) in source

    def test_later_authority_overrides_earlier_like_the_hook_does(
        self, tmp_path
    ) -> None:
        """common.sh: global ~/.omnibase/.env first, project .env overrides."""
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            resolve_hook_edge_lane,
        )

        first = tmp_path / "global.env"
        first.write_text("KAFKA_BOOTSTRAP_SERVERS=global-lane:39092\n")
        second = tmp_path / "project.env"
        second.write_text("KAFKA_BOOTSTRAP_SERVERS=project-lane:49092\n")

        lane, source = resolve_hook_edge_lane([first, second])

        assert lane == "project-lane:49092"
        assert str(second) in source

    def test_unresolvable_lane_refuses_instead_of_guessing(
        self, tmp_path, monkeypatch
    ) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            HookEdgeLaneUnresolvedError,
            resolve_hook_edge_lane,
        )

        monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "ambient-lane:19092")
        empty = tmp_path / "empty.env"
        empty.write_text("SOMETHING_ELSE=1\n")

        with pytest.raises(HookEdgeLaneUnresolvedError):
            resolve_hook_edge_lane([empty])

    def test_the_live_hook_edge_lane_differs_from_this_shells_ambient_lane(
        self,
    ) -> None:
        """The live fact this whole class exists for, asserted as a fact.

        Skipped when the two happen to agree -- agreement is not a defect, it
        just makes the regression unobservable on this host.
        """
        import os

        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            default_hook_edge_env_files,
            resolve_hook_edge_lane,
        )

        try:
            lane, source = resolve_hook_edge_lane(default_hook_edge_env_files())
        except Exception as exc:  # pragma: no cover - host without the authority
            pytest.skip(f"no hook edge authority on this host: {exc}")
        ambient = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
        if ambient is None:
            pytest.skip(
                "this suite scrubs KAFKA_BOOTSTRAP_SERVERS, so the ambient/authority "
                "divergence is not observable from inside it"
            )
        if ambient == lane:
            pytest.skip("ambient lane agrees with the hook edge lane on this host")
        assert lane != ambient, source


class TestForwarderLaneAttachmentIsMeasuredNotAsserted:
    """LANE_MISMATCH must rest on a measurement, and NO_CONSUMER must not.

    The forwarder's consumed lane is not declared in its contract and its
    runtime config loader needs cluster-side paths, so a string comparison of
    lanes can only ever compare a value against itself. The measurable fact
    available from the operator Mac is whether the forwarder's own
    contract-declared liveness topic is present and advancing ON the hook edge
    lane: absent means the forwarder is not attached to the lane the hooks
    publish to.
    """

    def test_forwarder_absent_from_the_emit_lane_is_a_lane_mismatch(self) -> None:
        result = classify_chain(
            correlation_id="cid-lane-1",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(),
            bus=_bus(),
            forwarder=ModelForwarderObservation(
                mirror_outbound_topics=OUTBOUND_ALLOWLIST,
                cloud_leg_transport="https_relay",
                forwarder_present_on_emit_lane=False,
                liveness_topic="onex.evt.omnibase-infra.gateway-canary.v1",
                consumer_group_advanced=False,
            ),
            gateway=None,
            projection=None,
        )
        assert result.failed_leg is EnumHookChainLeg.FORWARDER_RELAY
        assert result.primary_blocker is EnumHookChainBlocker.LANE_MISMATCH
        assert EnumHookChainBlocker.NO_CONSUMER not in result.blockers
        assert EnumHookChainBlocker.TIMEOUT not in result.blockers

    def test_forwarder_present_but_not_advancing_is_no_consumer(self) -> None:
        result = classify_chain(
            correlation_id="cid-lane-2",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(),
            bus=_bus(),
            forwarder=ModelForwarderObservation(
                mirror_outbound_topics=OUTBOUND_ALLOWLIST,
                cloud_leg_transport="https_relay",
                forwarder_present_on_emit_lane=True,
                liveness_topic="onex.evt.omnibase-infra.gateway-canary.v1",
                consumer_group_advanced=False,
            ),
            gateway=None,
            projection=None,
        )
        assert result.primary_blocker is EnumHookChainBlocker.NO_CONSUMER
        assert EnumHookChainBlocker.LANE_MISMATCH not in result.blockers

    def test_liveness_topic_is_read_from_the_forwarders_own_contract(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            load_forwarder_liveness_topic,
        )

        topic, detail = load_forwarder_liveness_topic(
            "omnibase_infra.nodes.node_bus_forwarder_effect"
        )
        assert detail is None, detail
        assert topic.startswith("onex.evt."), topic


class TestCloudLegsAgainstTheLiveSupplierRoute:
    """The cloud legs must read the route OMN-17205 actually serves.

    OMN-17205 merged ``GET /v1/projections/hook-events/by-correlation``
    (omninode_infra 93678ec, live on staging 17:42Z) as the supplier of exactly
    the string this node's contract declares. It takes the correlation id as a
    QUERY parameter and answers HTTP 200 for all three data states -- ``found``,
    ``not_found`` and ``projection_absent`` -- precisely so a missing leg-5 sink
    cannot read as a successful empty answer (the OMN-15797 silent-blinding
    class). A consumer that posts the id as a path segment, or that treats any
    non-empty 200 body as a row, re-opens that hole on the reading side and
    would let this probe report a green chain over a projection that does not
    exist.
    """

    def test_correlation_id_rides_as_a_query_param_not_a_path_segment(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            build_cloud_read_url,
        )

        url = build_cloud_read_url(
            base_url="https://gateway.example/",
            path="/v1/projections/hook-events/by-correlation",
            correlation_id="omn17202-abc123",
        )

        assert url == (
            "https://gateway.example/v1/projections/hook-events/by-correlation"
            "?correlation_id=omn17202-abc123"
        )

    def test_not_found_body_is_not_a_found_row(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            parse_projection_body,
        )

        found, data_state = parse_projection_body(
            '{"correlation_id":"x","projection":"hook_events",'
            '"data_state":"not_found","count":0,"rows":[]}'
        )

        assert found is False
        assert data_state == "not_found"

    def test_projection_absent_body_is_not_a_found_row(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            parse_projection_body,
        )

        found, data_state = parse_projection_body(
            '{"correlation_id":"x","projection":"hook_events",'
            '"data_state":"projection_absent","count":0,"rows":[]}'
        )

        assert found is False
        assert data_state == "projection_absent"

    def test_found_body_with_rows_is_a_found_row(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            parse_projection_body,
        )

        found, data_state = parse_projection_body(
            '{"correlation_id":"x","projection":"hook_events",'
            '"data_state":"found","count":1,"rows":[{"event_type":"tool-executed"}]}'
        )

        assert found is True
        assert data_state == "found"

    def test_missing_leg_five_sink_is_its_own_blocker_not_a_missing_row(self) -> None:
        """``projection_absent`` and ``not_found`` are different facts.

        The sink for leg 5 does not exist yet (OMN-17201). Collapsing "the
        table is not there" into "the row did not arrive" would point the
        operator at the relay when the defect is a table that was never built.
        """
        result = classify_chain(
            correlation_id="cid-sink",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True,
                status_code=200,
                correlation_found=True,
                route_served=True,
            ),
            projection=ModelCloudProjectionObservation(
                reachable=True,
                status_code=200,
                row_found=False,
                route_served=True,
                data_state="projection_absent",
            ),
        )

        assert result.failed_leg is EnumHookChainLeg.CLOUD_PROJECTION
        assert result.primary_blocker is EnumHookChainBlocker.PROJECTION_SINK_ABSENT

    def test_row_absent_from_an_existing_projection_stays_row_absent(self) -> None:
        result = classify_chain(
            correlation_id="cid-row",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True,
                status_code=200,
                correlation_found=True,
                route_served=True,
            ),
            projection=ModelCloudProjectionObservation(
                reachable=True,
                status_code=200,
                row_found=False,
                route_served=True,
                data_state="not_found",
            ),
        )

        assert result.failed_leg is EnumHookChainLeg.CLOUD_PROJECTION
        assert result.primary_blocker is EnumHookChainBlocker.PROJECTION_ROW_ABSENT


class TestAnAbsentRouteIsNotACredentialProblem:
    """401 on an unmatched path is the API's default, not a refusal.

    onex-api's M4 middleware 401s every unmatched ``/v1`` path, which is how
    OMN-17205 spent a session unable to tell "the read route refused me" from
    "the read route was never deployed" -- ``/v1/definitely-not-a-route-xyz``
    401s identically. The probe therefore reads the gateway's own public route
    list before believing a 401, and reports a route that is not served as
    exactly that.
    """

    def test_absent_gateway_ingest_route_is_not_reported_as_unauthorized(self) -> None:
        result = classify_chain(
            correlation_id="cid-route",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True,
                status_code=401,
                correlation_found=False,
                route_served=False,
            ),
            projection=None,
        )

        assert result.failed_leg is EnumHookChainLeg.CLOUD_GATEWAY
        assert result.primary_blocker is EnumHookChainBlocker.GATEWAY_ROUTE_ABSENT

    def test_a_served_route_returning_401_is_still_unauthorized(self) -> None:
        result = classify_chain(
            correlation_id="cid-401",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True,
                status_code=401,
                correlation_found=False,
                route_served=True,
            ),
            projection=None,
        )

        assert result.primary_blocker is EnumHookChainBlocker.GATEWAY_UNAUTHORIZED

    def test_route_presence_is_read_from_the_gateways_public_route_list(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            route_is_served,
        )

        served = route_is_served(
            openapi_body='{"paths": {"/v1/projections/hook-events/by-correlation": {}}}',
            path="/v1/projections/hook-events/by-correlation",
        )
        absent = route_is_served(
            openapi_body='{"paths": {"/v1/health": {}}}',
            path="/v1/hook-events/ingest",
        )
        unknown = route_is_served(openapi_body="<html>gateway error</html>", path="/x")

        assert served is True
        assert absent is False
        assert unknown is None, (
            "an unparseable route list must be UNKNOWN, never False -- claiming "
            "a route is absent because the list could not be read invents the "
            "same fabricated blocker this node exists to end"
        )

    def test_unknown_route_presence_never_fabricates_a_route_absent_verdict(
        self,
    ) -> None:
        result = classify_chain(
            correlation_id="cid-unknown",
            hook_topic="onex.evt.omniclaude.session-started.v1",
            emit=_emit(lane=DEV_LANE),
            bus=_bus(lane=DEV_LANE),
            forwarder=_forwarder(
                transport="https_relay", advanced=True, present_on_emit_lane=True
            ),
            gateway=ModelCloudGatewayObservation(
                reachable=True,
                status_code=401,
                correlation_found=False,
                route_served=None,
            ),
            projection=None,
        )

        assert result.primary_blocker is EnumHookChainBlocker.GATEWAY_UNAUTHORIZED


class TestTheContractDeclaresTheRouteItsSupplierServes:
    """The consumed path string must stay identical to the supplier's.

    OMN-17205 chose its route path specifically to match this contract; if this
    contract drifts, the two surfaces diverge silently and the leg-5 read starts
    401ing on an unmatched path again.
    """

    def test_cloud_projection_path_matches_the_supplier_route(self) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            _load_probe_config,
        )

        config = _load_probe_config()

        assert (
            config["cloud_projection_path"]
            == "/v1/projections/hook-events/by-correlation"
        )
        assert config["cloud_gateway_openapi_path"], (
            "the probe needs the gateway's public route list to tell an absent "
            "route from a refused one"
        )


class TestUnresolvableCloudAddressNamesTheConfigGap:
    """An unset resolver config is a config gap, not a filesystem error.

    ``resolve_secret_resolver_config_path()`` returns ``""`` when nothing is
    configured, and reading ``""`` raises ``IsADirectoryError`` -- which the
    probe would then have reported verbatim, sending a reader hunting for a
    corrupt file when the real fact is that this host has no secret-resolver
    config at all. Naming the gap is the difference between an actionable
    verdict and a misleading one.
    """

    def test_unset_resolver_config_is_named_as_the_gap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_hook_chain_probe_effect import live_probes

        monkeypatch.setattr(
            live_probes,
            "_resolve_secret_resolver_config_path",
            lambda: "",
        )

        base_url = live_probes.LiveHookChainProbes()._resolve_cloud_base_url()

        assert base_url.startswith("unresolved:")
        assert "no_secret_resolver_config" in base_url
        assert "IsADirectoryError" not in base_url
