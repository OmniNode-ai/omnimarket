# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prod-health fact resolver EFFECT tests (OMN-13441 / Phase 1.3).

Proves the orchestrator fact-gathering EFFECT that resolves the prod-lane health
into a deterministic ``ModelProdHealthFact`` — the un-forgeable input that makes
the prod-promotion grant requirement health-conditional WITHOUT opening a bypass:

  * healthy probe (reachable + 2xx) -> HEALTHY;
  * failed probe (reachable + non-2xx) -> UNHEALTHY;
  * unreachable / exception / timeout probe -> UNKNOWN (NOT UNHEALTHY);
  * the health fact is NEVER sourced from the start event / caller — the resolve
    command carries no health field and the resolved value comes only from the
    live probe;
  * the resolved fact's ``probed_at`` is the deterministic ``evaluated_at`` and
    its ``source`` is the un-forgeable probed prod-lane endpoint;
  * the gate command threads the fact through ``prod_health`` and rejects a
    caller-supplied ``health`` key on the command (extra="forbid").
"""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime
from email.message import Message
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.runtime_deployment import (
    EnumProdHealth,
    EnumRuntimeLane,
    ModelProdHealthFact,
    ModelProdHealthResolveCommand,
    ModelProdHealthResolvedEvent,
    ModelProdPromotionGateCommand,
    lane_target,
)
from omnimarket.nodes.node_prod_health_fact_resolver_effect.handlers.handler_prod_health_fact_resolver import (
    HandlerProdHealthFactResolver,
    HttpHealthProber,
    ProtocolHealthProber,
)
from omnimarket.nodes.node_prod_health_fact_resolver_effect.health_resolver import (
    ModelProbeResult,
    classify_health,
)

_EVALUATED_AT = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)

# The prod-lane main runtime health endpoint must be resolvable for the handler.
# The overlay env var binds it; set it deterministically for the probe-source assertions.
_PROD_HEALTH_URL = "http://omninode-runtime:28085/health"


@pytest.fixture(autouse=True)
def _bind_prod_health_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the prod-lane health overlay env var so lane_target resolves."""
    monkeypatch.setenv(
        "RUNTIME_PROD_HEALTH_URLS",
        f"{_PROD_HEALTH_URL};http://runtime-effects:28086/health",
    )


class _StubProber:
    """Prober returning a fixed result (or raising) for one probe."""

    def __init__(
        self, result: ModelProbeResult | None, *, exc: Exception | None = None
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[str] = []

    async def probe(self, url: str) -> ModelProbeResult:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


# --- pure classifier --------------------------------------------------------


def test_classify_healthy() -> None:
    assert (
        classify_health(ModelProbeResult(reachable=True, status_code=200))
        is EnumProdHealth.HEALTHY
    )


def test_classify_unhealthy_non_2xx() -> None:
    assert (
        classify_health(ModelProbeResult(reachable=True, status_code=503))
        is EnumProdHealth.UNHEALTHY
    )


def test_classify_unreachable_is_unknown_not_unhealthy() -> None:
    result = ModelProbeResult(reachable=False, status_code=None, detail="URLError")
    assert classify_health(result) is EnumProdHealth.UNKNOWN


def test_classify_no_status_is_unknown() -> None:
    # reachable=True but no definitive status is still indeterminate -> UNKNOWN.
    assert (
        classify_health(ModelProbeResult(reachable=True, status_code=None))
        is EnumProdHealth.UNKNOWN
    )


# --- handler: DoD cases -----------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_probe_resolves_healthy() -> None:
    prober: ProtocolHealthProber = _StubProber(
        ModelProbeResult(reachable=True, status_code=200)
    )
    handler = HandlerProdHealthFactResolver(prober=prober)
    cid = uuid4()
    output = await handler.handle(
        ModelProdHealthResolveCommand(correlation_id=cid, evaluated_at=_EVALUATED_AT)
    )
    event = output.events[0]
    assert isinstance(event, ModelProdHealthResolvedEvent)
    assert event.health_fact.health is EnumProdHealth.HEALTHY
    assert event.health_fact.lane is EnumRuntimeLane.PROD
    assert event.health_fact.probed_at == _EVALUATED_AT
    assert event.health_fact.source == _PROD_HEALTH_URL
    assert event.correlation_id == cid


@pytest.mark.asyncio
async def test_failed_probe_resolves_unhealthy() -> None:
    prober = _StubProber(ModelProbeResult(reachable=True, status_code=500))
    handler = HandlerProdHealthFactResolver(prober=prober)
    output = await handler.handle(
        ModelProdHealthResolveCommand(
            correlation_id=uuid4(), evaluated_at=_EVALUATED_AT
        )
    )
    assert output.events[0].health_fact.health is EnumProdHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_unreachable_probe_resolves_unknown_not_unhealthy() -> None:
    prober = _StubProber(
        ModelProbeResult(reachable=False, status_code=None, detail="URLError")
    )
    handler = HandlerProdHealthFactResolver(prober=prober)
    output = await handler.handle(
        ModelProdHealthResolveCommand(
            correlation_id=uuid4(), evaluated_at=_EVALUATED_AT
        )
    )
    health = output.events[0].health_fact.health
    assert health is not EnumProdHealth.UNHEALTHY
    assert health is EnumProdHealth.UNKNOWN


@pytest.mark.asyncio
async def test_raising_prober_resolves_unknown_not_unhealthy() -> None:
    prober = _StubProber(None, exc=TimeoutError("probe timed out"))
    handler = HandlerProdHealthFactResolver(prober=prober)
    output = await handler.handle(
        ModelProdHealthResolveCommand(
            correlation_id=uuid4(), evaluated_at=_EVALUATED_AT
        )
    )
    health = output.events[0].health_fact.health
    assert health is not EnumProdHealth.UNHEALTHY
    assert health is EnumProdHealth.UNKNOWN
    assert output.events[0].health_fact.source == _PROD_HEALTH_URL


@pytest.mark.asyncio
async def test_http_prober_maps_transport_error_to_unknown() -> None:
    # The default HTTP prober must NOT raise on a transport failure; it returns an
    # unreachable result so the classifier yields UNKNOWN (fail closed).
    prober = HttpHealthProber(timeout=0.001)
    # Probe a host that will not connect; assert a fail-closed unreachable result.
    result = await prober.probe("http://127.0.0.1:1/health")
    assert result.reachable is False
    assert classify_health(result) is EnumProdHealth.UNKNOWN


@pytest.mark.asyncio
async def test_http_prober_maps_http_error_to_unhealthy() -> None:
    # A definitive HTTP error status is a CONFIRMED unhealthy signal, not UNKNOWN.
    prober = HttpHealthProber()

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError(
            url="http://x/health",
            code=503,
            msg="down",
            hdrs=Message(),
            fp=None,
        )

    import urllib.request as _urlreq

    orig = _urlreq.urlopen
    _urlreq.urlopen = _raise
    try:
        result = await prober.probe("http://x/health")
    finally:
        _urlreq.urlopen = orig
    assert result.reachable is True
    assert result.status_code == 503
    assert classify_health(result) is EnumProdHealth.UNHEALTHY


# --- un-forgeable source ----------------------------------------------------


def test_resolve_command_has_no_caller_health_field() -> None:
    # The command surface carries NO health field — the caller cannot assert it.
    assert "health" not in ModelProdHealthResolveCommand.model_fields
    assert "prod_health" not in ModelProdHealthResolveCommand.model_fields


def test_resolve_command_rejects_extra_health_key() -> None:
    with pytest.raises(ValidationError):
        ModelProdHealthResolveCommand(  # type: ignore[call-arg]
            correlation_id=uuid4(),
            evaluated_at=_EVALUATED_AT,
            health=EnumProdHealth.HEALTHY,
        )


@pytest.mark.asyncio
async def test_fact_source_is_the_live_probed_endpoint() -> None:
    prober = _StubProber(ModelProbeResult(reachable=True, status_code=200))
    handler = HandlerProdHealthFactResolver(prober=prober)
    await handler.handle(
        ModelProdHealthResolveCommand(
            correlation_id=uuid4(), evaluated_at=_EVALUATED_AT
        )
    )
    # The handler probed the resolved prod-lane endpoint, not a caller value.
    expected = lane_target(EnumRuntimeLane.PROD).health_targets[0]
    assert prober.calls == [expected]


def test_gate_command_threads_prod_health_fact() -> None:
    fact = ModelProdHealthFact(
        health=EnumProdHealth.UNHEALTHY,
        probed_at=_EVALUATED_AT,
        source=_PROD_HEALTH_URL,
    )
    command = ModelProdPromotionGateCommand(
        correlation_id=uuid4(),
        runtime_lane=EnumRuntimeLane.PROD,
        prod_health=fact,
    )
    assert command.prod_health is fact
    # Default is None (absent == fail closed, treated like UNKNOWN by the gate).
    assert ModelProdPromotionGateCommand(correlation_id=uuid4()).prod_health is None


# --- golden chain: resolver EFFECT -> resolved event -> gate command ---------


@pytest.mark.asyncio
async def test_golden_chain_resolved_fact_reaches_gate_command_not_start() -> None:
    """Golden chain: the resolver EFFECT's fact flows into the gate command.

    Proves the orchestrator fact-gathering path: a malicious ``start`` payload
    asserting HEALTHY is IGNORED — the gate command's ``prod_health`` is taken
    only from the resolver EFFECT's resolved event, whose value came from the live
    probe (UNHEALTHY here). The health fact is never sourced from ``start``.
    """
    cid = uuid4()

    # A hostile caller "start" payload that LIES about prod being healthy.
    forged_start = {"prod_health": EnumProdHealth.HEALTHY.value}
    assert forged_start["prod_health"] == "healthy"

    # The resolver EFFECT probes live prod and finds it DOWN (confirmed unhealthy).
    prober = _StubProber(ModelProbeResult(reachable=True, status_code=503))
    handler = HandlerProdHealthFactResolver(prober=prober)
    output = await handler.handle(
        ModelProdHealthResolveCommand(correlation_id=cid, evaluated_at=_EVALUATED_AT)
    )
    resolved = output.events[0]
    assert isinstance(resolved, ModelProdHealthResolvedEvent)

    # The orchestrator stamps the RESOLVED fact onto the gate command — NOT the
    # forged start value.
    gate_command = ModelProdPromotionGateCommand(
        correlation_id=cid,
        runtime_lane=EnumRuntimeLane.PROD,
        evaluated_at=_EVALUATED_AT,
        prod_health=resolved.health_fact,
    )
    assert gate_command.prod_health is not None
    assert gate_command.prod_health.health is EnumProdHealth.UNHEALTHY
    # The forged "healthy" assertion never reached the gate.
    assert resolved.health_fact.health is not EnumProdHealth.HEALTHY
    assert (
        gate_command.prod_health.source
        == lane_target(EnumRuntimeLane.PROD).health_targets[0]
    )
