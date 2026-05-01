# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for DaemonHealthCheckTarget."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.models.model_daemon_health_probe_result import (
    ModelDaemonHealthProbeResult,
)
from omnimarket.nodes.node_process_watchdog.handlers.check_daemon_health import (
    DaemonHealthCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)


@pytest.mark.unit
def test_daemon_health_target_passes_probe_result_metadata() -> None:
    calls: list[tuple[str, str, str | None, float, Path | None]] = []

    def probe_func(
        socket_path: str,
        bootstrap_servers: str,
        correlation_id: str | None,
        timeout_s: float,
        *,
        event_registry_path: Path | None = None,
    ) -> ModelDaemonHealthProbeResult:
        calls.append(
            (
                socket_path,
                bootstrap_servers,
                correlation_id,
                timeout_s,
                event_registry_path,
            )
        )
        return ModelDaemonHealthProbeResult(
            success=True,
            correlation_id="corr-health",
            reason="daemon health probe round trip succeeded",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
            topic="onex.evt.diagnostic.daemon-health.v1",
            event_id="evt-health",
            kafka_offset=42,
            round_trip_ms=12.5,
        )

    registry_path = Path("/tmp/topics.yaml")
    target = DaemonHealthCheckTarget(
        socket_path="/tmp/emit.sock",
        bootstrap_servers="localhost:9092",
        correlation_id="corr-health",
        timeout_s=2.0,
        event_registry_path=registry_path,
        probe_func=probe_func,
    )

    result = target.check()

    assert calls == [
        ("/tmp/emit.sock", "localhost:9092", "corr-health", 2.0, registry_path)
    ]
    assert target.category == EnumCheckTarget.EMIT_DAEMON
    assert result.status == EnumCheckStatus.HEALTHY
    assert result.message == "daemon health probe round trip succeeded"
    assert result.details["round_trip_ms"] == 12.5
    assert result.details["kafka_offset"] == 42
    assert result.details["event_id"] == "evt-health"
    assert result.details["topic"] == "onex.evt.diagnostic.daemon-health.v1"


@pytest.mark.unit
def test_daemon_health_target_fails_when_probe_fails() -> None:
    def probe_func(
        socket_path: str,
        bootstrap_servers: str,
        correlation_id: str | None,
        timeout_s: float,
        *,
        event_registry_path: Path | None = None,
    ) -> ModelDaemonHealthProbeResult:
        _ = correlation_id, timeout_s, event_registry_path
        return ModelDaemonHealthProbeResult(
            success=False,
            correlation_id="corr-down",
            reason="socket missing: /tmp/emit.sock",
            socket_path=socket_path,
            bootstrap_servers=bootstrap_servers,
        )

    target = DaemonHealthCheckTarget(
        socket_path="/tmp/emit.sock",
        bootstrap_servers="localhost:9092",
        probe_func=probe_func,
    )

    result = target.check()

    assert result.status == EnumCheckStatus.DOWN
    assert result.message == "socket missing: /tmp/emit.sock"
    assert result.details["correlation_id"] == "corr-down"
    assert "round_trip_ms" not in result.details
    assert "kafka_offset" not in result.details


@pytest.mark.unit
def test_daemon_health_target_reports_probe_exceptions_as_unknown() -> None:
    def probe_func(
        socket_path: str,
        bootstrap_servers: str,
        correlation_id: str | None,
        timeout_s: float,
        *,
        event_registry_path: Path | None = None,
    ) -> ModelDaemonHealthProbeResult:
        _ = (
            socket_path,
            bootstrap_servers,
            correlation_id,
            timeout_s,
            event_registry_path,
        )
        raise RuntimeError("bad probe")

    target = DaemonHealthCheckTarget(
        socket_path="/tmp/emit.sock",
        bootstrap_servers="localhost:9092",
        probe_func=probe_func,
    )

    result = target.check()

    assert result.status == EnumCheckStatus.UNKNOWN
    assert result.message == "Daemon health probe error: bad probe"
    assert result.details == {
        "socket_path": "/tmp/emit.sock",
        "bootstrap_servers": "localhost:9092",
    }


@pytest.mark.unit
def test_daemon_health_target_restart_is_disabled() -> None:
    target = DaemonHealthCheckTarget(
        socket_path="/tmp/emit.sock",
        bootstrap_servers="localhost:9092",
    )

    assert target.restart() is False
