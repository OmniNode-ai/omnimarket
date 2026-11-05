# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runner cutoff regression through the node's typed dispatch and in-memory bus."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runner_orchestrator/contract.yaml"
)
NOW = datetime(2026, 11, 5, 8, 15, tzinfo=UTC)
pytestmark = pytest.mark.unit


def deployment(tmp_path: Path, *, offline: bool = False) -> dict[str, Any]:
    """Recorded version, offline state and recovery version; synthetic pool identities."""
    pin = tmp_path / "runner-image.lock.json"
    pin.write_text(json.dumps({"runner_version": "2.336.0"}))
    state = tmp_path / "runner-fleet.json"
    state.write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "runners": [
                    {
                        "runner_name": "ci-a",
                        "status": "offline" if offline else "online",
                    },
                    {"runner_name": "ci-b", "status": "busy"},
                ],
            }
        )
    )
    return {
        "pin_path": str(pin),
        "exception_state_dir": str(tmp_path / "exception-queue"),
        "releases_url": "https://example.invalid/runner/releases",
        "request_timeout_seconds": 2,
        "pools": [
            {
                "pool_id": "ci",
                "fleet_state_path": str(state),
                "listeners": [
                    {
                        "runner_name": name,
                        "version_command": [sys.executable, "-c", "print('2.336.0')"],
                    }
                    for name in ("ci-a", "ci-b")
                ],
            }
        ],
    }


def releases() -> bytes:
    return json.dumps(
        [
            {"tag_name": "v2.338.2", "draft": False, "prerelease": False},
            {"tag_name": "v2.338.1", "draft": False, "prerelease": False},
            {"tag_name": "v2.337.0", "draft": False, "prerelease": False},
            {"tag_name": "v2.336.0", "draft": False, "prerelease": False},
        ]
    ).encode()


async def chain(
    overlay: dict[str, Any],
    *,
    release_error: bool = False,
    contract_path: Path = CONTRACT,
    release_payload: bytes | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors
    contract = yaml.safe_load(contract_path.read_text())
    topics = contract["runner_version_watch"]["topics"]
    bus = EventBusInmemory()
    completed: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []

    def read_releases(url: str, timeout: float) -> bytes:
        assert url == overlay["releases_url"]
        assert timeout == overlay["request_timeout_seconds"]
        if release_error:
            raise OSError("recorded release source unavailable")
        return releases() if release_payload is None else release_payload

    async def terminal(message: Any) -> None:
        document = json.loads(message.value)
        completed.append(document.get("payload", document))

    async def alarm(message: Any) -> None:
        alarms.append(json.loads(message.value))

    await bus.start()
    try:
        await bus.subscribe(
            topics["completed"], on_message=terminal, group_id="terminal-test"
        )
        await bus.subscribe(topics["alarm"], on_message=alarm, group_id="alarm-test")
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
            materialized_explicit_dependencies={
                "HandlerRunnerVersionWatch": {
                    "event_bus": bus,
                    "read_releases": read_releases,
                    "contract_path": contract_path,
                }
            },
        )
        assert report.total_failed == 0
        engine.freeze()
        correlation_id = str(uuid4())
        await bus.publish(
            topics["command"],
            key=None,
            value=json.dumps(
                {
                    "payload": {
                        "now": NOW.isoformat(),
                        "correlation_id": correlation_id,
                        "overlay": overlay,
                    },
                    "correlation_id": correlation_id,
                    "event_type": "omnimarket.runner-version-watch-requested",
                }
            ).encode(),
        )
        assert len(completed) == 1
        assert completed[0]["correlation_id"] == correlation_id
        return completed[0], alarms
    finally:
        await bus.close()


async def test_golden_chain_warns_before_cutoff_and_recovers(tmp_path: Path) -> None:
    overlay = deployment(tmp_path)
    result, alarms = await chain(overlay)
    assert result["status"] == "alarm"
    assert result["latest_version"] == "2.338.2"
    assert result["pin_version"] == "2.336.0"
    assert {finding["code"] for finding in result["findings"]} == {
        "behind_latest",
        "pin_behind_latest",
    }
    assert {
        finding["runner_name"]
        for finding in result["findings"]
        if finding["code"] == "behind_latest"
    } == {"ci-a", "ci-b"}
    assert len(alarms) == 1
    assert alarms[0]["findings"] == result["findings"]
    Path(overlay["pin_path"]).write_text(json.dumps({"runner_version": "2.338.2"}))
    for listener in overlay["pools"][0]["listeners"]:
        listener["version_command"][-1] = "print('2.338.2')"
    healthy, recovered_alarms = await chain(overlay)
    assert healthy["status"] == "healthy"
    assert healthy["findings"] == []
    assert recovered_alarms == []


async def test_error_chain_source_failure_keeps_offline_alarm(tmp_path: Path) -> None:
    overlay = deployment(tmp_path, offline=True)
    result, alarms = await chain(overlay, release_error=True)
    assert result["status"] == "error"
    assert {finding["code"] for finding in result["findings"]} == {
        "offline",
        "release_source_unreadable",
    }
    assert result["pools_checked"] == ["ci"]
    assert len(alarms) == 1
    assert alarms[0]["status"] == "error"


@pytest.fixture
def release_server() -> Any:
    """Exercise the production HTTP adapter on this host without a GitHub read."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    response = {"status": 200, "body": releases()}

    class ReleaseEndpoint(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response["body"])

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReleaseEndpoint)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/releases", response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("http_status", [200, 503])
async def test_runtime_tick_http_listener_and_attention_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_server: Any,
    http_status: int,
) -> None:
    """Boot the production manifest, fire its tick, and read operator queue rows."""

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import (
        EventBusInmemory as RuntimeBus,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

    url, response = release_server
    response["status"] = http_status
    overlay = deployment(tmp_path, offline=True)
    overlay["releases_url"] = url
    overlay_dir = tmp_path / "overlays" / "runner_version_watch"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "overlay.yaml").write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_dir.parent))
    exception_contract = (
        CONTRACT.parent.parent / "node_exception_queue_effect/contract.yaml"
    )
    manifest = discover_contracts_from_paths([CONTRACT, exception_contract])
    assert not manifest.errors, manifest.errors
    bus = RuntimeBus(environment="runner-watch-test", group="runner-watch-test")
    await bus.start()
    topics = yaml.safe_load(CONTRACT.read_text())["runner_version_watch"]["topics"]
    completed: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def collect(message: Any) -> None:
        document = json.loads(message.value)
        completed.append(document.get("payload", document))
        done.set()

    await bus.subscribe(topics["completed"], on_message=collect, group_id="proof")
    engine = MessageDispatchEngine()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0, report
        assert report.total_wired == 2
        engine.freeze()
        correlation_id = uuid4()
        tick = ModelRuntimeTick(
            now=NOW,
            scheduled_at=NOW,
            tick_id=uuid4(),
            correlation_id=correlation_id,
            sequence_number=1,
            scheduler_id="watch-proof",
            tick_interval_ms=1000,
        )
        envelope = ModelEventEnvelope[object](
            payload=tick.model_dump(mode="json"),
            correlation_id=correlation_id,
            event_type="platform.runtime-tick",
            envelope_timestamp=NOW,
        )
        tick_topic = yaml.safe_load(CONTRACT.read_text())["input_subscriptions"][0][
            "topic"
        ]
        await bus.publish(tick_topic, None, envelope.model_dump_json().encode(), None)
        await asyncio.wait_for(done.wait(), timeout=10)
        assert len(completed) == 1
        action_topic = yaml.safe_load(CONTRACT.read_text())["terminal_event"]
        assert await bus.get_event_history(topic=action_topic) == []
        assert completed[0]["correlation_id"] == str(correlation_id)
        assert completed[0]["status"] == ("alarm" if http_status == 200 else "error")
        assert "offline" in {f["code"] for f in completed[0]["findings"]}
        queue_file = Path(overlay["exception_state_dir"]) / "exception-queue.jsonl"
        assert queue_file.is_file(), (
            "alarm did not reach the existing operator attention sink"
        )
        rows = [json.loads(line) for line in queue_file.read_text().splitlines()]
        assert any(
            row.get("decision") and "offline" in row.get("key", "") for row in rows
        )
    finally:
        await bus.close()


async def test_off_pin_alarms_even_when_not_behind(tmp_path: Path) -> None:
    overlay = deployment(tmp_path)
    Path(overlay["pin_path"]).write_text(json.dumps({"runner_version": "2.338.2"}))
    for listener in overlay["pools"][0]["listeners"]:
        listener["version_command"][-1] = "print('2.338.1')"
    result, alarms = await chain(overlay)
    assert result["status"] == "alarm"
    assert {f["code"] for f in result["findings"]} == {"off_pin"}
    assert len(alarms) == 1


@pytest.mark.parametrize("age_seconds", [601, -31])
async def test_stale_or_future_online_state_is_an_error(
    tmp_path: Path,
    age_seconds: int,
) -> None:
    from datetime import timedelta

    overlay = deployment(tmp_path)
    path = Path(overlay["pools"][0]["fleet_state_path"])
    document = json.loads(path.read_text())
    document["observed_at"] = (NOW - timedelta(seconds=age_seconds)).isoformat()
    path.write_text(json.dumps(document))
    result, alarms = await chain(overlay)
    assert result["status"] == "error"
    assert "fleet_state_stale" in {f["code"] for f in result["findings"]}
    assert len(alarms) == 1


async def test_missing_runner_and_listener_error_do_not_hide_another_pool(
    tmp_path: Path,
) -> None:
    import copy

    overlay = deployment(tmp_path)
    path = Path(overlay["pools"][0]["fleet_state_path"])
    document = json.loads(path.read_text())
    document["runners"] = document["runners"][:1]
    path.write_text(json.dumps(document))
    overlay["pools"][0]["listeners"][0]["version_command"][-1] = "raise SystemExit(2)"
    other = copy.deepcopy(overlay["pools"][0])
    other["pool_id"] = "secondary-ci"
    other["listeners"] = [
        {
            "runner_name": "secondary-a",
            "version_command": [sys.executable, "-c", "print('2.338.2')"],
        }
    ]
    overlay["pools"].append(other)
    result, _ = await chain(overlay)
    assert result["status"] == "error"
    assert result["pools_checked"] == ["ci", "secondary-ci"]
    found = {(f["code"], f["runner_name"]) for f in result["findings"]}
    assert ("listener_unreadable", "ci-a") in found
    assert ("offline", "ci-b") in found
    assert ("off_pin", "secondary-a") in found


async def test_contract_policy_controls_exact_release_lag_boundary(
    tmp_path: Path,
) -> None:
    overlay = deployment(tmp_path)
    Path(overlay["pin_path"]).write_text(json.dumps({"runner_version": "2.337.0"}))
    for listener in overlay["pools"][0]["listeners"]:
        listener["version_command"][-1] = "print('2.337.0')"
    # This stable version is two releases behind. The shipped limit of one alarms.
    alarmed, _ = await chain(overlay)
    assert alarmed["status"] == "alarm"
    assert {f["releases_behind"] for f in alarmed["findings"]} == {2}
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["runner_version_watch"]["policy"]["max_releases_behind"] = 2
    alternate = tmp_path / "contract.yaml"
    alternate.write_text(yaml.safe_dump(contract))
    healthy, alarms = await chain(overlay, contract_path=alternate)
    assert healthy["status"] == "healthy"
    assert alarms == []


async def test_drafts_and_prereleases_do_not_age_a_healthy_listener(
    tmp_path: Path,
) -> None:
    overlay = deployment(tmp_path)
    Path(overlay["pin_path"]).write_text(json.dumps({"runner_version": "2.338.2"}))
    for listener in overlay["pools"][0]["listeners"]:
        listener["version_command"][-1] = "print('2.338.2')"
    history = json.loads(releases())
    history.extend(
        [
            {"tag_name": "v9.0.0-preview", "draft": False, "prerelease": True},
            {"tag_name": "v8.0.0", "draft": True, "prerelease": False},
        ]
    )
    result, alarms = await chain(overlay, release_payload=json.dumps(history).encode())
    assert result["status"] == "healthy"
    assert result["latest_version"] == "2.338.2"
    assert alarms == []


@pytest.mark.parametrize("corrupt", ["pin", "state", "releases"])
async def test_malformed_inputs_are_reported(tmp_path: Path, corrupt: str) -> None:
    overlay = deployment(tmp_path)
    payload = None
    if corrupt == "pin":
        Path(overlay["pin_path"]).write_text("{}")
        expected = "pin_unreadable"
    elif corrupt == "state":
        Path(overlay["pools"][0]["fleet_state_path"]).write_text("not json")
        expected = "fleet_state_unreadable"
    else:
        payload = b"[]"
        expected = "release_source_unreadable"
    result, alarms = await chain(overlay, release_payload=payload)
    assert result["status"] == "error"
    assert expected in {f["code"] for f in result["findings"]}
    assert len(alarms) == 1


async def test_delivery_failure_cannot_emit_a_success_terminal(tmp_path: Path) -> None:
    from omnimarket.nodes.node_runner_orchestrator.handlers.handler_runner_version_watch import (
        HandlerRunnerVersionWatch,
    )
    from omnimarket.nodes.node_runner_orchestrator.models.model_runner_version_watch import (
        ModelRunnerWatchRequest,
    )

    class RefusingBus(EventBusInmemory):
        def __init__(self) -> None:
            super().__init__()
            self.topics: list[str] = []

        async def publish(self, topic: str, key: object, value: bytes) -> None:
            self.topics.append(topic)
            raise OSError("recorded bus publish refusal")

    bus = RefusingBus()
    handler = HandlerRunnerVersionWatch(
        event_bus=bus, read_releases=lambda _url, _timeout: releases()
    )
    with pytest.raises(OSError, match="recorded bus publish refusal"):
        await handler.handle(
            ModelRunnerWatchRequest(
                correlation_id=uuid4(), now=NOW, overlay=deployment(tmp_path)
            )
        )
    declared = yaml.safe_load(CONTRACT.read_text())["runner_version_watch"]["topics"]
    assert bus.topics == [declared["alarm"]]


async def test_off_slot_runtime_tick_never_reads_or_publishes() -> None:
    from datetime import timedelta

    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

    from omnimarket.nodes.node_runner_orchestrator.handlers.handler_runner_version_watch_tick import (
        HandlerRunnerVersionWatchTick,
    )

    bus = EventBusInmemory()
    await bus.start()
    try:
        tick = ModelRuntimeTick(
            now=NOW + timedelta(seconds=1),
            scheduled_at=NOW + timedelta(seconds=1),
            tick_id=uuid4(),
            correlation_id=uuid4(),
            sequence_number=2,
            scheduler_id="off-slot-proof",
            tick_interval_ms=1000,
        )
        assert await HandlerRunnerVersionWatchTick(event_bus=bus).handle(tick) is None
        assert await bus.get_event_history() == []
    finally:
        await bus.close()


async def test_overlay_absence_emits_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_runner_orchestrator.handlers.handler_runner_version_watch import (
        HandlerRunnerVersionWatch,
    )
    from omnimarket.nodes.node_runner_orchestrator.models.model_runner_version_watch import (
        ModelRunnerWatchRequest,
    )

    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    bus = EventBusInmemory()
    await bus.start()
    try:
        result = await HandlerRunnerVersionWatch(event_bus=bus).handle(
            ModelRunnerWatchRequest(correlation_id=uuid4(), now=NOW)
        )
        assert result.status == "error"
        assert [f.code for f in result.findings] == ["configuration_unreadable"]
        declared = yaml.safe_load(CONTRACT.read_text())["runner_version_watch"][
            "topics"
        ]
        assert [message.topic for message in await bus.get_event_history()] == [
            declared["alarm"]
        ]
    finally:
        await bus.close()


async def test_unknown_online_state_is_unreadable_not_measured_offline(
    tmp_path: Path,
) -> None:
    overlay = deployment(tmp_path)
    path = Path(overlay["pools"][0]["fleet_state_path"])
    document = json.loads(path.read_text())
    document["runners"][0]["status"] = "unknown"
    path.write_text(json.dumps(document))
    result, _ = await chain(overlay)
    assert result["status"] == "error"
    codes = {f["code"] for f in result["findings"] if f["runner_name"] == "ci-a"}
    assert "fleet_state_unreadable" in codes
    assert "offline" not in codes
