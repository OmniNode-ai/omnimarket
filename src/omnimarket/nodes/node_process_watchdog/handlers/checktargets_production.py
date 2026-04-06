"""Production CheckTarget implementations for .201 infrastructure.

Each class implements the CheckTarget protocol from handler_process_watchdog.
These targets make real HTTP/socket/subprocess calls.

Usage:
  from omnimarket.nodes.node_process_watchdog.handlers.checktargets_production import (
      build_production_targets,
  )
  targets = build_production_targets()
  report = handler.run_checks(command, targets)
"""

from __future__ import annotations

import logging
import socket
import subprocess

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)

logger = logging.getLogger(__name__)

_EMIT_DAEMON_HOST = "127.0.0.1"
_EMIT_DAEMON_PORT = 9877

_LLM_ENDPOINTS: list[tuple[str, int]] = [
    ("vllm-primary", 8000),
    ("vllm-secondary", 8001),
    ("vllm-tertiary", 8100),
    ("vllm-quaternary", 8101),
]


class EmitDaemonCheckTarget:
    """Check emit daemon socket health on localhost:9877."""

    @property
    def name(self) -> str:
        return "emit_daemon"

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.EMIT_DAEMON

    def check(self) -> ModelWatchdogCheckResult:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((_EMIT_DAEMON_HOST, _EMIT_DAEMON_PORT))
            sock.close()
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.HEALTHY,
                message="Emit daemon socket accepting connections",
            )
        except (OSError, TimeoutError) as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"Emit daemon unreachable: {e}",
            )

    def restart(self) -> bool:
        logger.warning("Emit daemon restart not implemented — requires supervisor")
        return False


class KafkaConsumerCheckTarget:
    """Check Kafka consumer group membership and lag via rpk."""

    def __init__(self, consumer_group: str = "omnidash-consumers-v2") -> None:
        self._group = consumer_group

    @property
    def name(self) -> str:
        return f"kafka_consumer_{self._group}"

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.KAFKA_CONSUMERS

    def check(self) -> ModelWatchdogCheckResult:
        try:
            result = subprocess.run(
                ["rpk", "group", "describe", self._group, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self.category,
                    status=EnumCheckStatus.DOWN,
                    message=f"rpk failed: {result.stderr.strip()[:200]}",
                )

            output = result.stdout.strip()
            if '"members":[]' in output or '"members": []' in output:
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self.category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Consumer group {self._group} has 0 members",
                    details={"group": self._group, "members": 0},
                )

            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.HEALTHY,
                message=f"Consumer group {self._group} is active",
            )

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"Cannot check Kafka: {e}",
            )

    def restart(self) -> bool:
        logger.warning("Kafka consumer restart not supported via watchdog")
        return False


class LlmEndpointCheckTarget:
    """Check LLM endpoint /health via HTTP."""

    def __init__(self, label: str, port: int, host: str = "127.0.0.1") -> None:
        self._label = label
        self._port = port
        self._host = host

    @property
    def name(self) -> str:
        return f"llm_{self._label}_{self._port}"

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.LLM_ENDPOINTS

    def check(self) -> ModelWatchdogCheckResult:
        try:
            import urllib.request

            url = f"http://{self._host}:{self._port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return ModelWatchdogCheckResult(
                        target=self.name,
                        category=self.category,
                        status=EnumCheckStatus.HEALTHY,
                        message=f"LLM endpoint {self._label}:{self._port} healthy",
                    )
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self.category,
                    status=EnumCheckStatus.DEGRADED,
                    message=f"LLM endpoint returned status {resp.status}",
                )
        except Exception as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"LLM endpoint unreachable: {e}",
            )

    def restart(self) -> bool:
        logger.warning("LLM endpoint restart not supported via watchdog")
        return False


class DockerContainerCheckTarget:
    """Check Docker container health on .201."""

    def __init__(self, container_name: str) -> None:
        self._container = container_name

    @property
    def name(self) -> str:
        return f"docker_{self._container}"

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.DOCKER_CONTAINERS

    def check(self) -> ModelWatchdogCheckResult:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}",
                    self._container,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status_str = result.stdout.strip()
            if result.returncode != 0:
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self.category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Container {self._container} not found",
                )
            if status_str == "running":
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self.category,
                    status=EnumCheckStatus.HEALTHY,
                    message=f"Container {self._container} running",
                )
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"Container {self._container} status: {status_str}",
                details={"status": status_str},
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"Cannot check Docker: {e}",
            )

    def restart(self) -> bool:
        try:
            result = subprocess.run(
                ["docker", "restart", self._container],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


def build_production_targets() -> list[object]:
    """Build the full set of production check targets for .201."""
    targets: list[object] = [
        EmitDaemonCheckTarget(),
        KafkaConsumerCheckTarget("omnidash-consumers-v2"),
    ]
    for label, port in _LLM_ENDPOINTS:
        targets.append(LlmEndpointCheckTarget(label, port))
    targets.append(DockerContainerCheckTarget("redpanda"))
    targets.append(DockerContainerCheckTarget("postgres"))
    targets.append(DockerContainerCheckTarget("emit-daemon"))
    return targets


__all__: list[str] = [
    "DockerContainerCheckTarget",
    "EmitDaemonCheckTarget",
    "KafkaConsumerCheckTarget",
    "LlmEndpointCheckTarget",
    "build_production_targets",
]
