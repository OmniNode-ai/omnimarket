"""DockerApiCheckTarget — Docker SDK container health check.

Uses the Docker SDK (docker-py) to inspect container state via the Docker
daemon API. Prefer over subprocess ``docker inspect`` for structured data
and proper error handling.

Status mapping:
  HEALTHY  — container running and (if configured) health check passing
  DEGRADED — container running but health status is "unhealthy" or "starting"
  DOWN     — container not found, not running, or Docker daemon unreachable
  UNKNOWN  — unexpected error
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)

logger = logging.getLogger(__name__)


class DockerApiCheckTarget:
    def __init__(
        self,
        container_name: str,
        category: EnumCheckTarget = EnumCheckTarget.DOCKER_CONTAINERS,
    ) -> None:
        if not container_name:
            raise ValueError("container_name must be non-empty")
        self._container_name = container_name
        self._category = category

    @property
    def name(self) -> str:
        return f"docker_api:{self._container_name}"

    @property
    def category(self) -> EnumCheckTarget:
        return self._category

    def check(self) -> ModelWatchdogCheckResult:
        try:
            import docker

            client = docker.from_env()
            container = client.containers.get(self._container_name)
            container.reload()
            state = container.attrs.get("State", {})
            status = state.get("Status", "unknown")
            health = state.get("Health", {})
            health_status = health.get("Status") if health else None

            if status != "running":
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Container {self._container_name} status: {status}",
                    details={"status": status},
                )

            if health_status == "unhealthy":
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DEGRADED,
                    message=f"Container {self._container_name} health: {health_status}",
                    details={"status": status, "health": health_status},
                )

            if health_status == "starting":
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DEGRADED,
                    message=f"Container {self._container_name} health: {health_status}",
                    details={"status": status, "health": health_status},
                )

            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.HEALTHY,
                message=f"Container {self._container_name} running",
                details={"status": status, "health": health_status},
            )
        except ImportError:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message="Docker SDK (docker-py) not installed",
            )
        except Exception as e:
            error_msg = str(e)
            if "No such container" in error_msg or "not found" in error_msg.lower():
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Container {self._container_name} not found",
                )
            if (
                "connection refused" in error_msg.lower()
                or "cannot connect" in error_msg.lower()
            ):
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Docker daemon unreachable: {e}",
                )
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"Docker check error: {e}",
            )

    def restart(self) -> bool:
        try:
            import docker

            client = docker.from_env()
            container = client.containers.get(self._container_name)
            container.restart(timeout=10)
            return True
        except Exception:
            logger.warning("Docker restart failed for %s", self._container_name)
            return False


__all__: list[str] = ["DockerApiCheckTarget"]
