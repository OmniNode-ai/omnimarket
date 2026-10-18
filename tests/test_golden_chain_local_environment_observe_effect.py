# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Catalog -> runtime tick -> observer -> runtime publisher -> status topic."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.local_deployment.tenant_identity import (
    EnumLocalTenantIdentityRefusalReason,
    LocalTenantIdentityError,
    ModelLocalTenantIdentityRefusal,
)
from omnimarket.models.model_local_environment_status import ModelLocalEnvironmentStatus
from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
NODE = "node_local_environment_observe_effect"
INPUT_TOPIC = "onex.intent.platform.runtime-tick.v1"
OUTPUT_TOPIC = "onex.evt.omnimarket.local-environment-status.v1"
TENANT = "19988000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    services = tmp_path / "services"
    services.mkdir()
    (tmp_path / "bundles.yaml").write_text(
        yaml.safe_dump({"local": {"services": ["http", "tcp", "one-shot", "gate"]}})
    )
    for name in ("http", "tcp", "one-shot", "gate"):
        manifest: dict[str, object] = {
            "name": name,
            "image": "test:local",
            "layer": "runtime",
        }
        if name != "gate":
            manifest["ports"] = {"external": 19000, "internal": 8000}
        if name == "http":
            manifest["healthcheck"] = {
                "test": "curl -f http://localhost:8001/health?ready=1 || exit 1"
            }
        if name == "one-shot":
            manifest["restart"] = "no"
        (services / f"{name}.yaml").write_text(yaml.safe_dump(manifest))
    return tmp_path


class FakeProber:
    def __init__(self, http_state: str, tcp_state: str) -> None:
        self.states = {"http": http_state, "tcp": tcp_state}
        self.calls: list[observer.ModelProbeTarget] = []
        self.both_started = asyncio.Event()

    async def probe(
        self, target: observer.ModelProbeTarget, timeout: float
    ) -> observer.ModelProbeResult:
        assert timeout == 3
        self.calls.append(target)
        if len(self.calls) % 2 == 0:
            self.both_started.set()
        # A sequential implementation times out instead of reaching the bus.
        await asyncio.wait_for(self.both_started.wait(), timeout=1)
        return observer.ModelProbeResult(
            observed_state=EnumLocalServiceState(self.states[target.service]),
            reason=f"Probed {target.service}:{target.port}; fake returned {self.states[target.service]}.",
        )


def tick_bytes(seconds: int) -> bytes:
    now = NOW + timedelta(seconds=seconds)
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=seconds + 1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="local-environment-observe-test",
        tick_interval_ms=1000,
    )
    envelope = ModelEventEnvelope[object](
        payload=tick.model_dump(mode="json"), correlation_id=tick.correlation_id
    )
    return envelope.model_dump_json().encode()


async def wired_bus(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, prober: FakeProber
) -> tuple[EventBusInmemory, list[ModelHandlerOutput[None]]]:
    config = observer.ModelLocalEnvironmentObserveConfig(catalog_dir=str(catalog))
    monkeypatch.setattr(observer, "_load_contract_config", lambda: config)
    monkeypatch.setattr(observer, "RealNetworkProber", lambda: prober)
    monkeypatch.setattr(
        observer, "resolve_local_deployment_tenant_id", lambda _: TENANT
    )
    handled: list[ModelHandlerOutput[None]] = []
    real_handle = observer.HandlerLocalEnvironmentObserve.handle

    async def recording_handle(
        self: observer.HandlerLocalEnvironmentObserve, request: ModelRuntimeTick
    ) -> ModelHandlerOutput[None]:
        result = await real_handle(self, request)
        handled.append(result)
        return result

    monkeypatch.setattr(
        observer.HandlerLocalEnvironmentObserve, "handle", recording_handle
    )
    manifest = discover_contracts_from_paths([NODES / NODE / "contract.yaml"])
    assert manifest.total_errors == 0, manifest.errors
    bus = EventBusInmemory(environment="test", group="local-environment-observe")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == 1
    assert report.total_failed == 0
    engine.freeze()
    return bus, handled


async def statuses(bus: EventBusInmemory) -> list[ModelLocalEnvironmentStatus]:
    return [
        ModelLocalEnvironmentStatus.model_validate(json.loads(message.value)["payload"])
        for message in await bus.get_event_history(topic=OUTPUT_TOPIC)
    ]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_state", "tcp_state"),
    [("running", "running"), ("unhealthy", "down"), ("down", "running")],
)
async def test_catalog_tick_to_status_bus_and_interval(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, http_state: str, tcp_state: str
) -> None:
    prober = FakeProber(http_state, tcp_state)
    bus, handled = await wired_bus(monkeypatch, catalog, prober)
    try:
        await bus.publish(INPUT_TOPIC, None, tick_bytes(0), None)
        reports = await statuses(bus)
        assert len(handled) == 1
        assert len(reports) == 2
        assert {r.service: r.observed_state.value for r in reports} == {
            "http": http_state,
            "tcp": tcp_state,
        }
        for report in reports:
            assert report.tenant_id == TENANT
            assert report.mode.value == "compose"
            assert report.observed_at == NOW
            assert report.reason
            assert report.versions == {}
        targets = {target.service: target for target in prober.calls}
        assert targets["http"].port == 8001
        assert targets["http"].path == "/health"
        assert targets["http"].query == "ready=1"
        assert targets["http"].scheme == "http"
        assert targets["tcp"].port == 8000
        assert targets["tcp"].scheme is None
        logs = " ".join(handled[0].logs)
        assert "one-shot" in logs
        assert "restart" in logs
        assert "gate" in logs
        assert "port" in logs

        await bus.publish(INPUT_TOPIC, None, tick_bytes(14), None)
        assert len(handled) == 2
        assert handled[-1].events == ()
        assert len(await statuses(bus)) == 2
        assert len(prober.calls) == 2

        await bus.publish(INPUT_TOPIC, None, tick_bytes(15), None)
        assert len(handled) == 3
        assert len(await statuses(bus)) == 4
        assert len(prober.calls) == 4
        assert all(
            r.observed_at == NOW + timedelta(seconds=15)
            for r in (await statuses(bus))[2:]
        )
    finally:
        await bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [None, "a-slug", TENANT.upper() + " "])
async def test_no_canonical_tenant_means_no_probes_or_events(
    monkeypatch: pytest.MonkeyPatch, catalog: Path, tenant: str | None
) -> None:
    prober = FakeProber("running", "running")
    bus, handled = await wired_bus(monkeypatch, catalog, prober)

    def resolve(_: str | None) -> str:
        if tenant is None:
            raise LocalTenantIdentityError(
                ModelLocalTenantIdentityRefusal(
                    reason=EnumLocalTenantIdentityRefusalReason.IDENTITY_ABSENT,
                    detail="This test install has no tenant identity.",
                )
            )
        return tenant

    monkeypatch.setattr(observer, "resolve_local_deployment_tenant_id", resolve)
    try:
        await bus.publish(INPUT_TOPIC, None, tick_bytes(0), None)
        assert len(handled) == 1
        assert handled[0].events == ()
        assert "identity_" in " ".join(handled[0].logs)
        assert prober.calls == []
        assert await statuses(bus) == []
    finally:
        await bus.close()


def test_observer_produces_the_topic_consumed_by_the_projection(tmp_path: Path) -> None:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir()
    roots["omnimarket"] = NODES.parent
    graph = build_graph(roots=roots)
    assert any(NODE in name for name in graph.producers.get(OUTPUT_TOPIC, ()))
    assert any(
        "projection_local_environment" in name
        for name in graph.consumers.get(OUTPUT_TOPIC, ())
    )
    contract = yaml.safe_load((NODES / NODE / "contract.yaml").read_text())
    assert contract["terminal_event"] == OUTPUT_TOPIC


class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        code = 503 if path == "/failure" else 200
        if query != "token=SECRET":
            code = 400
        body = b'{"status":"degraded"}' if path == "/degraded" else b'{"status":"ok"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_prober_with_real_http_tcp_and_closed_port() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    prober = observer.RealNetworkProber()
    try:
        for path, expected in (
            ("/health", "running"),
            ("/degraded", "unhealthy"),
            ("/failure", "unhealthy"),
        ):
            result = await prober.probe(
                observer.ModelProbeTarget(
                    service="127.0.0.1",
                    port=server.server_port,
                    scheme="http",
                    path=path,
                    query="token=SECRET",
                ),
                1,
            )
            assert result.observed_state.value == expected
            assert "SECRET" not in result.reason
            assert "token=" not in result.reason
            assert path in result.reason
            assert str(503 if path == "/failure" else 200) in result.reason
        for port, expected in (
            (tcp.getsockname()[1], "running"),
            (closed_port, "down"),
        ):
            result = await prober.probe(
                observer.ModelProbeTarget(service="127.0.0.1", port=port), 1
            )
            assert result.observed_state.value == expected
            assert str(port) in result.reason
        result = await prober.probe(
            observer.ModelProbeTarget(
                service="127.0.0.1",
                port=closed_port,
                scheme="http",
                path="/health",
                query="token=SECRET",
            ),
            1,
        )
        assert result.observed_state.value == "down"
        assert "SECRET" not in result.reason
    finally:
        tcp.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
