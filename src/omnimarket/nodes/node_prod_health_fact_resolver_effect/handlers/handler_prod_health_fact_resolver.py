# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_prod_health_fact_resolver_effect (OMN-13441 / Phase 1.3).

EFFECT node. The orchestrator fact-gathering boundary that resolves the prod-lane
health into a deterministic ``ModelProdHealthFact`` BEFORE the prod gate
evaluates — the un-forgeable input that makes the prod-promotion grant
requirement health-conditional WITHOUT opening a bypass.

Un-forgeable source: the health value comes from a LIVE prod-lane health probe at
evaluation time (resolved via ``lane_target(PROD).health_targets``), never from a
caller-supplied field. A redeploy request therefore cannot assert its own health.

Fail-closed: any indeterminate probe (unreachable / error / timeout / no status)
classifies to ``EnumProdHealth.UNKNOWN`` (NOT ``UNHEALTHY``) so the gate still
requires an approver grant — breaking the probe cannot induce the recovery-waiver
path. Only a CONFIRMED healthy probe yields ``HEALTHY``; only a CONFIRMED
down/failed probe yields ``UNHEALTHY``.

The handler:
  1. resolves the prod-lane health endpoint(s) from the deploy target;
  2. probes the endpoint via the injected prober (HTTP GET at the effect boundary);
  3. classifies the probe result through the pure ``classify_health``;
  4. stamps ``probed_at`` from the command's deterministic ``evaluated_at`` and
     records the probed endpoint as the un-forgeable ``source``;
  5. emits ``ModelProdHealthResolvedEvent`` carrying the resolved fact.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelProdHealthFact,
    ModelProdHealthResolveCommand,
    ModelProdHealthResolvedEvent,
    lane_target,
)
from omnimarket.nodes.node_prod_health_fact_resolver_effect.health_resolver import (
    ModelProbeResult,
    classify_health,
)

_HANDLER_ID = "node_prod_health_fact_resolver_effect"
_PROBE_TIMEOUT = 10.0


class ProtocolHealthProber(Protocol):
    """The I/O boundary that probes one prod-lane health endpoint.

    Injected so the EFFECT is testable without network: tests supply a prober
    returning a fixed ``ModelProbeResult`` (or raising, to prove the fail-closed
    UNKNOWN path); the deployed boundary issues an HTTP GET against the live
    prod-lane health endpoint.
    """

    async def probe(self, url: str) -> ModelProbeResult:
        """Probe a health endpoint, returning a structural (unclassified) result."""
        ...


class HttpHealthProber:
    """Default prober: HTTP GET against the prod-lane health endpoint.

    Any transport exception (connection refused, timeout, DNS failure, HTTP
    error) is caught and mapped to an UNREACHABLE ``ModelProbeResult`` so the pure
    classifier resolves it to ``UNKNOWN`` (fail closed). The prober NEVER raises —
    an exception here must not crash the EFFECT into a state the gate would read
    as anything other than UNKNOWN.
    """

    def __init__(self, timeout: float = _PROBE_TIMEOUT) -> None:
        self._timeout = timeout

    async def probe(self, url: str) -> ModelProbeResult:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = int(response.status)
            return ModelProbeResult(reachable=True, status_code=status)
        except urllib.error.HTTPError as exc:
            # A definitive HTTP error status IS a confirmed (unhealthy) signal.
            return ModelProbeResult(
                reachable=True, status_code=int(exc.code), detail=str(exc)
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # No definitive status -> indeterminate -> UNKNOWN (fail closed).
            return ModelProbeResult(
                reachable=False, status_code=None, detail=type(exc).__name__
            )


class HandlerProdHealthFactResolver:
    """EFFECT: resolve the prod-lane health into a deterministic fact.

    A prober may be injected for tests; otherwise the HTTP prober is composed at
    ``handle()`` time. The resolved fact's ``health`` fails closed to ``UNKNOWN``
    on any indeterminate probe.
    """

    def __init__(self, prober: ProtocolHealthProber | None = None) -> None:
        self._prober = prober

    async def handle(
        self, command: ModelProdHealthResolveCommand
    ) -> ModelHandlerOutput[None]:
        """Probe prod-lane health and emit the resolved fact."""
        prober = self._prober if self._prober is not None else HttpHealthProber()
        target = lane_target(EnumRuntimeLane.PROD)
        # The main runtime health endpoint is the canonical liveness signal.
        health_url = target.health_targets[0]

        result = await prober.probe(health_url)
        health = classify_health(result)

        fact = ModelProdHealthFact(
            health=health,
            probed_at=command.evaluated_at,
            source=health_url,
        )
        event = ModelProdHealthResolvedEvent(
            correlation_id=command.correlation_id,
            health_fact=fact,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(event,),
        )


__all__: list[str] = [
    "HandlerProdHealthFactResolver",
    "HttpHealthProber",
    "ProtocolHealthProber",
]
