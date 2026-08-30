# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler for the hook->cloud correlation probe (OMN-17202).

One invocation, one correlation id, five legs. The handler owns ONLY the I/O
boundary and the short-circuit: it walks the legs in order and stops probing the
moment one fails, so a downstream leg is reported ``NOT_ATTEMPTED`` rather than
being blamed for an upstream break. All judgement lives in the pure
``chain_classifier``.

The boundary is a single injected protocol so the whole chain is testable
without a broker, a gateway, or cluster access -- and so the operator path
(AC4) is the same code path the tests pin.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from omnimarket.nodes.node_hook_chain_probe_effect.chain_classifier import (
    classify_chain,
)
from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    ModelCloudGatewayObservation,
    ModelCloudProjectionObservation,
    ModelForwarderObservation,
    ModelHookChainAddress,
    ModelHookChainProbeRequest,
    ModelHookChainProbeResult,
    ModelLocalBusObservation,
    ModelLocalEmitObservation,
)

#: Correlation ids are prefixed so a probe record is identifiable in any log or
#: projection it reaches without needing this node's output to interpret it.
_CORRELATION_PREFIX = "omn17202"


class ProtocolHookChainProbes(Protocol):
    """The five-leg I/O boundary.

    Implementations resolve addressing from contracts (never hardcoded topics or
    URLs) and perform exactly one observation per leg.
    """

    async def resolve_address(self) -> ModelHookChainAddress:
        """Resolve hook topic, emit lane and cloud gateway base URL from contracts."""
        ...

    async def emit(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelLocalEmitObservation:
        """Leg 1: emit one hook-shaped event carrying the correlation id."""
        ...

    async def read_local_bus(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelLocalBusObservation:
        """Leg 2: read the correlated record back off the local bus."""
        ...

    async def read_forwarder(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelForwarderObservation:
        """Leg 3: read the forwarder's declared mirror policy and live consumption."""
        ...

    async def read_cloud_gateway(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelCloudGatewayObservation:
        """Leg 4: ask the cloud gateway whether it ingested the correlation id."""
        ...

    async def read_cloud_projection(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelCloudProjectionObservation:
        """Leg 5: read the correlated row back out of the cloud projection."""
        ...


class HandlerHookChainProbe:
    """EFFECT: trace one correlated hook event across all five legs."""

    def __init__(self, probes: ProtocolHookChainProbes | None = None) -> None:
        """``probes`` may be injected for tests; the live boundary is composed
        lazily at ``handle()`` time so importing this module never opens a
        broker connection."""
        self._probes = probes

    def _boundary(self, timeout_seconds: float) -> ProtocolHookChainProbes:
        if self._probes is not None:
            return self._probes
        from omnimarket.nodes.node_hook_chain_probe_effect.live_probes import (
            LiveHookChainProbes,
        )

        return LiveHookChainProbes(timeout_seconds=timeout_seconds)

    async def handle(
        self, payload: ModelHookChainProbeRequest
    ) -> ModelHookChainProbeResult:
        """Emit one correlated event and report the furthest leg it reached.

        ``payload`` is named to match the runtime's single-parameter dispatch
        convention; the runtime wraps the single typed return into the dispatch
        envelope.
        """
        correlation_id = (
            payload.correlation_id or f"{_CORRELATION_PREFIX}-{uuid4().hex}"
        )
        probes = self._boundary(payload.timeout_seconds)
        address = await probes.resolve_address()

        emit = await probes.emit(correlation_id=correlation_id, address=address)
        bus: ModelLocalBusObservation | None = None
        forwarder: ModelForwarderObservation | None = None
        gateway: ModelCloudGatewayObservation | None = None
        projection: ModelCloudProjectionObservation | None = None

        if emit.emitted:
            bus = await probes.read_local_bus(
                correlation_id=correlation_id, address=address
            )
        if bus is not None and bus.observed:
            forwarder = await probes.read_forwarder(
                correlation_id=correlation_id, address=address
            )

        interim = classify_chain(
            correlation_id=correlation_id,
            hook_topic=address.hook_topic,
            emit=emit,
            bus=bus,
            forwarder=forwarder,
            gateway=None,
            projection=None,
        )
        forwarder_reached = any(
            leg.reached for leg in interim.legs if leg.leg.value == "forwarder_relay"
        )

        if forwarder_reached:
            gateway = await probes.read_cloud_gateway(
                correlation_id=correlation_id, address=address
            )
            if gateway.reachable and gateway.correlation_found:
                projection = await probes.read_cloud_projection(
                    correlation_id=correlation_id, address=address
                )

        return classify_chain(
            correlation_id=correlation_id,
            hook_topic=address.hook_topic,
            emit=emit,
            bus=bus,
            forwarder=forwarder,
            gateway=gateway,
            projection=projection,
        )


__all__: list[str] = ["HandlerHookChainProbe", "ProtocolHookChainProbes"]
