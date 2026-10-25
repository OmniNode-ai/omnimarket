# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Take the preferred host away between two real deploy-effect publications."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    HandlerDeployPublishMonitor,
)


class RecordingBus:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    async def publish(self, topic: str, **kwargs: Any) -> None:
        self.commands.append(json.loads(kwargs["value"]))


def health(instance: str, now: datetime, *, priority: int = 0) -> dict[str, Any]:
    return {
        "instance": instance,
        "repository": "omnimarket",
        "observed_at": now.isoformat(),
        "accepting": True,
        "verify_ready": True,
        "priority": priority,
        "verify": {
            "receipt_lane": f"proof-{instance}",
            "runner_labels": ["self-hosted", "verify", instance],
            "deploy_agent_url": f"http://{instance}.example:8098",
            "main_url": f"http://{instance}.example:8085",
            "effects_url": f"http://{instance}.example:8086",
            "projection_url": f"http://{instance}.example:3002",
            "compose_project": f"runtime-{instance}",
            "runtime_container": "runtime",
            "effects_container": "effects",
            "postgres_container": "postgres",
            "broker_container": "broker",
        },
    }


def seed(path: Path, readings: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "health": readings, "decisions": {}})
    )


def command() -> ModelDeployPublishCommand:
    return ModelDeployPublishCommand(
        correlation_id=uuid4(),
        dev_deploy_protocol="health-v1",
        runtime_lane=EnumRuntimeLane.DEV,
        requested_by="gha/omnimarket/pr-example",
        git_ref="origin/dev",
    )


@pytest.mark.unit
async def test_departed_host_moves_the_next_deploy_to_the_other_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    path = tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json"
    now = datetime.now(UTC)
    seed(path, [health("worker-a", now), health("worker-b", now, priority=1)])
    bus = RecordingBus()
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands[-1].get("dev_deploy_route", {}).get("instance") == "worker-a"

    # Only the vanished machine's heartbeat changes. No route or deployment config changes.
    seed(
        path,
        [
            health("worker-a", now - timedelta(minutes=10)),
            health("worker-b", now, priority=1),
        ],
    )
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    route = bus.commands[-1].get("dev_deploy_route", {})
    assert route.get("instance") == "worker-b"
    assert route["verify"]["receipt_lane"] == "proof-worker-b"
    assert route["verify"]["runner_labels"][-1] == "worker-b"


@pytest.mark.unit
@pytest.mark.parametrize("age", [90, 91, -1])
async def test_refuses_stale_boundary_and_future_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.dev_deploy_routing_store import (
        DevDeployRoutingStore,
    )

    now = datetime.now(UTC)
    path = tmp_path / "routes.json"
    seed(path, [health("worker-a", now - timedelta(seconds=age))])
    with pytest.raises(ValueError, match="no fresh"):
        DevDeployRoutingStore(path).choose(
            uuid4(), request_fingerprint="request", repository="omnimarket", now=now
        )


@pytest.mark.unit
@pytest.mark.parametrize("field", ["accepting", "verify_ready"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_refuses_non_bool_health_flags(field: str, value: Any) -> None:
    from pydantic import ValidationError

    from omnimarket.models.model_dev_deploy_route import ModelDevDeployHealth

    reading = health("worker-a", datetime.now(UTC))
    reading[field] = value
    with pytest.raises(ValidationError):
        ModelDevDeployHealth.model_validate(reading)


@pytest.mark.unit
@pytest.mark.parametrize("field", ["accepting", "verify_ready"])
async def test_unavailable_host_is_skipped_without_a_default_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    unavailable = health("worker-a", now)
    unavailable[field] = False
    seed(
        tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json",
        [unavailable, health("worker-b", now, priority=1)],
    )
    bus = RecordingBus()
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands[-1]["dev_deploy_route"]["instance"] == "worker-b"


@pytest.mark.unit
async def test_refuses_an_empty_pool_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    bus = RecordingBus()
    with pytest.raises(ValueError, match="no fresh"):
        await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands == []


@pytest.mark.unit
async def test_new_host_and_recovered_preferred_host_need_no_route_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.models.model_dev_deploy_route import ModelDevDeployHealth
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.dev_deploy_routing_store import (
        DevDeployRoutingStore,
    )

    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    path = tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json"
    seed(path, [health("worker-a", now - timedelta(minutes=10))])
    store = DevDeployRoutingStore(path)
    store.record(
        ModelDevDeployHealth.model_validate(health("worker-new", now, priority=1))
    )
    bus = RecordingBus()
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands[-1]["dev_deploy_route"]["instance"] == "worker-new"
    store.record(ModelDevDeployHealth.model_validate(health("worker-a", now)))
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands[-1]["dev_deploy_route"]["instance"] == "worker-a"


@pytest.mark.unit
async def test_a_failed_publish_retry_retains_one_acceptor_after_health_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.models.model_dev_deploy_route import (
        ModelDevDeployHealth,
        ModelDevDeployRoute,
    )
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.dev_deploy_routing_store import (
        DevDeployRoutingStore,
    )

    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    path = tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json"
    seed(path, [health("worker-a", now), health("worker-b", now, priority=1)])

    class AmbiguousPublishBus(RecordingBus):
        async def publish(self, topic: str, **kwargs: Any) -> None:
            await super().publish(topic, **kwargs)
            raise ConnectionError("publication may already have reached the broker")

    failed_bus = AmbiguousPublishBus()
    request = command()
    with pytest.raises(ConnectionError):
        await HandlerDeployPublishMonitor(failed_bus).publish_rebuild_command(request)
    unavailable = health("worker-a", datetime.now(UTC))
    unavailable["accepting"] = False
    DevDeployRoutingStore(path).record(ModelDevDeployHealth.model_validate(unavailable))
    retried_bus = RecordingBus()
    await HandlerDeployPublishMonitor(retried_bus).publish_rebuild_command(request)
    before = failed_bus.commands[-1]["dev_deploy_route"]
    after = retried_bus.commands[-1]["dev_deploy_route"]
    assert before == after
    route = ModelDevDeployRoute.model_validate(after)
    assert sum(route.accepts(name) for name in ("worker-a", "worker-b")) == 1
    assert route.verify_outputs()["receipt_lane"] == route.verify.receipt_lane
    assert json.loads(route.verify_outputs()["verify_runs_on"])[-1] == route.instance


@pytest.mark.unit
async def test_refuses_corrupt_routing_state_before_any_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    path = tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json")
    bus = RecordingBus()
    with pytest.raises(ValueError, match="Invalid JSON"):
        await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands == []


@pytest.mark.unit
async def test_health_event_is_durable_and_cannot_regress_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        TOPIC_DEV_DEPLOY_HEALTH,
    )

    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    now = datetime.now(UTC)
    bus = RecordingBus()
    for reading in [
        health("worker-a", now),
        health("worker-a", now - timedelta(minutes=10)),
    ]:
        await HandlerDeployPublishMonitor(bus).handle(
            ModelEventEnvelope(
                payload=reading,
                event_type=TOPIC_DEV_DEPLOY_HEALTH,
                correlation_id=uuid4(),
            )
        )
    assert bus.commands == []
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    assert bus.commands[-1]["dev_deploy_route"]["instance"] == "worker-a"


@pytest.mark.unit
async def test_signed_command_binds_the_route_and_matching_verify_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import hmac

    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", "unit-test-signing-secret")
    now = datetime.now(UTC)
    seed(
        tmp_path / "redeploy_deploy_effect" / "dev_deploy_routing.json",
        [health("worker-a", now)],
    )
    bus = RecordingBus()
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(command())
    wire = dict(bus.commands[-1])
    signature = wire.pop("_signature")
    body = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    assert hmac.compare_digest(
        signature,
        hmac.new(b"unit-test-signing-secret", body, hashlib.sha256).hexdigest(),
    )
    wire["dev_deploy_route"]["instance"] = "worker-b"
    changed = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    assert not hmac.compare_digest(
        signature,
        hmac.new(b"unit-test-signing-secret", changed, hashlib.sha256).hexdigest(),
    )


@pytest.mark.unit
async def test_legacy_request_omits_the_new_protocol_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    bus = RecordingBus()
    legacy = command().model_copy(update={"dev_deploy_protocol": None})
    await HandlerDeployPublishMonitor(bus).publish_rebuild_command(legacy)
    assert "dev_deploy_route" not in bus.commands[-1]


@pytest.mark.unit
async def test_health_protocol_survives_the_orchestrator_gate_hop() -> None:
    from tests.test_redeploy_requested_at_omn19270 import _start, _through_the_gate

    request = _start().model_copy(update={"dev_deploy_protocol": "health-v1"})
    assert _through_the_gate(request).dev_deploy_protocol == "health-v1"
