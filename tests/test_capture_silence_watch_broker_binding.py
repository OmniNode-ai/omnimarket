# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24342: the capture silence watch dials the broker its overlay declares, never KAFKA_BROKERS.

Seams read here: the node's name against its overlay directory (the lookup the timer entry does),
the timer's non-dry publish against the declared broker, and the unit declaration against the
workspace root the lane table is read from. The failing control is the dev head: it reads
KAFKA_BROKERS, so test_publish_dials_the_declared_broker fails there.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    resolve_node_event_bus,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_capture_silence_watch_effect import __main__ as cli
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    ModelActorNewest,
    ModelCaptureObservation,
    ModelCaptureSource,
    ModelCaptureWatchEvaluation,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
NODE = "node_capture_silence_watch_effect"
UNITS_CONTRACT = NODES / "node_host_installer_registry_units" / "contract.yaml"
NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)
HANDLER_MODULE = importlib.import_module(HandlerCaptureSilenceWatch.__module__)


class _Clock:
    def __call__(self) -> float:
        return NOW.timestamp()


class _SilentActorReader:
    """A fresh source with one principal silent past its limit, so one alarm is due."""

    def read(
        self, source: ModelCaptureSource, *, active_since: datetime
    ) -> ModelCaptureObservation:
        return ModelCaptureObservation(
            source_newest_at=NOW,
            actors=(
                ModelActorNewest(
                    actor="principal-a", newest_at=NOW - timedelta(seconds=15000)
                ),
            ),
        )


class _FakeProducer:
    instances: list[_FakeProducer] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[tuple[str, bytes]] = []
        self.stopped = False
        _FakeProducer.instances.append(self)

    async def start(self) -> None:
        return None

    async def send_and_wait(self, topic: str, payload: bytes) -> None:
        self.sent.append((topic, payload))

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )


@pytest.fixture
def silent_actor_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    def build_handler(*, event_publisher: Any, host: str) -> HandlerCaptureSilenceWatch:
        return HandlerCaptureSilenceWatch(
            reader=_SilentActorReader(),
            event_publisher=event_publisher,
            clock=_Clock(),
            host=host,
        )

    monkeypatch.setattr(cli, "build_handler", build_handler)


def _declared_broker() -> ModelNodeBroker:
    return ModelNodeBroker(
        kind="kafka",
        lane="dev",
        bootstrap_servers="declared.invalid:9092",
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="PLAIN",
        sasl_username="declared-user",
        sasl_password=SecretStr("declared-secret"),
    )


def test_publish_dials_the_declared_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    import aiokafka

    _FakeProducer.instances.clear()
    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _FakeProducer)
    monkeypatch.setattr(
        cli, "resolve_dialable_broker", lambda _node: _declared_broker()
    )
    monkeypatch.setenv("KAFKA_BROKERS", "env.invalid:1")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "env.invalid:2")
    asyncio.run(cli._publish("onex.evt.omnimarket.x.v1", b"{}", dry_run=False))
    (producer,) = _FakeProducer.instances
    assert producer.kwargs == {
        "bootstrap_servers": "declared.invalid:9092",
        "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "PLAIN",
        "sasl_plain_username": "declared-user",
        "sasl_plain_password": "declared-secret",
    }
    assert [topic for topic, _ in producer.sent]
    assert producer.stopped


def test_no_declared_broker_ends_exit_3_with_every_alarm_unpublished(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    silent_actor_tick: None,
) -> None:
    def refuse(_node: str) -> ModelNodeBroker:
        raise NodeBrokerResolutionError("no event_bus declared")

    monkeypatch.setattr(cli, "resolve_dialable_broker", refuse)
    monkeypatch.setenv("KAFKA_BROKERS", "env.invalid:1")
    assert cli.main(["tick"]) == 3
    captured = capsys.readouterr()
    evaluation = ModelCaptureWatchEvaluation.model_validate_json(captured.out)
    assert evaluation.deliveries
    assert all(not delivery.published for delivery in evaluation.deliveries)
    assert captured.err == ""


def test_dry_run_never_resolves_a_broker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    silent_actor_tick: None,
) -> None:
    def refuse(_node: str) -> ModelNodeBroker:
        raise AssertionError("a dry run must not resolve a broker")

    monkeypatch.setattr(cli, "resolve_dialable_broker", refuse)
    assert cli.main(["tick", "--dry-run"]) == 2
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any("slack-publish" in event["topic"] for event in events)


def test_overlay_lookup_finds_the_node_by_its_directory_name(tmp_path: Path) -> None:
    node_dir = tmp_path / "overlays" / cli.NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        "event_bus:\n  type: kafka\n  lane: dev\n", encoding="utf-8"
    )
    bus, _path = resolve_node_event_bus(
        cli.NODE_NAME,
        None,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"), "HOME": str(tmp_path)},
    )
    assert bus is not None
    assert (bus.type, bus.lane) == ("kafka", "dev")
    assert cli.NODE_NAME == NODE
    assert (NODES / NODE / "contract.yaml").is_file()


def _unit(raw: dict[str, Any], key: str) -> dict[str, Any] | None:
    if isinstance(raw.get(key), dict):
        return raw[key]
    for value in raw.values():
        if isinstance(value, dict) and (found := _unit(value, key)) is not None:
            return found
    return None


def test_unit_declares_the_workspace_root_and_names_no_broker() -> None:
    raw = yaml.safe_load(UNITS_CONTRACT.read_text(encoding="utf-8"))
    unit = _unit(raw, "capture-silence-watch")
    assert unit is not None
    environment = unit["environment"]
    assert environment["ONEX_WORKSPACE_CONFIG_ROOT"] == "{workspace_config_root}"
    assert not [name for name in environment if name.startswith("KAFKA_")]
    assert unit["module"].startswith(f"omnimarket.nodes.{NODE}.")
