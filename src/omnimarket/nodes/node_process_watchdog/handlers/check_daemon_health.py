# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""DaemonHealthCheckTarget for end-to-end emit daemon health checks."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_emit_daemon.health_probe import probe
from omnimarket.nodes.node_emit_daemon.models.model_daemon_health_probe_result import (
    ModelDaemonHealthProbeResult,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)


class ProbeFunction(Protocol):
    def __call__(
        self,
        socket_path: str,
        bootstrap_servers: str,
        correlation_id: str | None,
        timeout_s: float,
        *,
        event_registry_path: Path | None = None,
    ) -> ModelDaemonHealthProbeResult: ...


class DaemonHealthCheckTarget:
    """Check emit daemon liveness with a socket-to-Kafka health probe."""

    def __init__(
        self,
        socket_path: str,
        bootstrap_servers: str,
        *,
        name: str = "daemon_health",
        correlation_id: str | None = None,
        timeout_s: float = 5.0,
        event_registry_path: Path | None = None,
        probe_func: ProbeFunction = probe,
    ) -> None:
        if not name:
            raise ValueError("name must be non-empty")
        if not socket_path:
            raise ValueError("socket_path must be non-empty")
        if not bootstrap_servers:
            raise ValueError("bootstrap_servers must be non-empty")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be greater than zero")
        self._name = name
        self._socket_path = socket_path
        self._bootstrap_servers = bootstrap_servers
        self._correlation_id = correlation_id
        self._timeout_s = timeout_s
        self._event_registry_path = event_registry_path
        self._probe_func = probe_func

    @property
    def name(self) -> str:
        return self._name

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.EMIT_DAEMON

    def check(self) -> ModelWatchdogCheckResult:
        try:
            result = self._probe_func(
                self._socket_path,
                self._bootstrap_servers,
                self._correlation_id,
                self._timeout_s,
                event_registry_path=self._event_registry_path,
            )
        except Exception as exc:
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self.category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"Daemon health probe error: {exc}",
                details={
                    "socket_path": self._socket_path,
                    "bootstrap_servers": self._bootstrap_servers,
                },
            )

        return ModelWatchdogCheckResult(
            target=self._name,
            category=self.category,
            status=(
                EnumCheckStatus.HEALTHY if result.success else EnumCheckStatus.DOWN
            ),
            message=result.reason,
            details=_result_details(result),
        )

    def restart(self) -> bool:
        return False


def _result_details(result: ModelDaemonHealthProbeResult) -> dict[str, object]:
    details: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "socket_path": result.socket_path,
        "bootstrap_servers": result.bootstrap_servers,
        "checked_at": result.checked_at.isoformat(),
    }
    if result.topic is not None:
        details["topic"] = result.topic
    if result.event_id is not None:
        details["event_id"] = result.event_id
    if result.kafka_offset is not None:
        details["kafka_offset"] = result.kafka_offset
    if result.round_trip_ms is not None:
        details["round_trip_ms"] = result.round_trip_ms
    return details


__all__: list[str] = ["DaemonHealthCheckTarget"]
