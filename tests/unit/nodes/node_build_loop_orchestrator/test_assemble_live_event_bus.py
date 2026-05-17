"""Live build-loop event-bus bootstrap tests."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.config.settings import Settings


def _load_build_event_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[Settings], ProtocolEventBusPublisher]:
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(omni_home))
    module = importlib.import_module(
        "omnimarket.nodes.node_build_loop_orchestrator.assemble_live"
    )
    return module._build_event_bus


@pytest.mark.unit
def test_build_event_bus_requires_kafka_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        kafka_bootstrap_servers="",
        kafka_broker="",
    )
    build_event_bus = _load_build_event_bus(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        build_event_bus(settings)


@pytest.mark.unit
def test_build_event_bus_uses_typed_kafka_config_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        kafka_bootstrap_servers="redpanda:9092",
        kafka_environment="prod",
    )
    build_event_bus = _load_build_event_bus(monkeypatch, tmp_path)

    bus = build_event_bus(settings)

    assert isinstance(bus, EventBusKafka)
    assert bus.config.bootstrap_servers == "redpanda:9092"
    assert bus.config.environment == "prod"


@pytest.mark.unit
def test_build_event_bus_accepts_typed_kafka_broker_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        kafka_bootstrap_servers="",
        kafka_broker="localhost:19092",
    )
    build_event_bus = _load_build_event_bus(monkeypatch, tmp_path)

    bus = build_event_bus(settings)

    assert isinstance(bus, EventBusKafka)
    assert bus.config.bootstrap_servers == "localhost:19092"
