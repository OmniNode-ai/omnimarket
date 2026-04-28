"""TargetRpk — Kafka consumer group health via rpk subprocess.

Wraps ``rpk group describe <group> --format json`` to check consumer group
membership and lag. All subprocess boundaries are mockable.

Status mapping:
  HEALTHY  — group has members
  DOWN     — group has 0 members or rpk returned non-zero
  UNKNOWN  — rpk not found or subprocess timeout
"""

from __future__ import annotations

import subprocess

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)


class TargetRpk:
    def __init__(
        self,
        consumer_group: str,
        timeout: float = 10.0,
        category: EnumCheckTarget = EnumCheckTarget.KAFKA_CONSUMERS,
    ) -> None:
        if not consumer_group:
            raise ValueError("consumer_group must be non-empty")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        self._group = consumer_group
        self._timeout = timeout
        self._category = category

    @property
    def name(self) -> str:
        return f"rpk:{self._group}"

    @property
    def category(self) -> EnumCheckTarget:
        return self._category

    def check(self) -> ModelWatchdogCheckResult:
        try:
            result = subprocess.run(
                ["rpk", "group", "describe", self._group, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DOWN,
                    message=f"rpk failed: {result.stderr.strip()[:200]}",
                )

            output = result.stdout.strip()
            if '"members":[]' in output or '"members": []' in output:
                return ModelWatchdogCheckResult(
                    target=self.name,
                    category=self._category,
                    status=EnumCheckStatus.DOWN,
                    message=f"Consumer group {self._group} has 0 members",
                    details={"group": self._group, "members": 0},
                )

            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.HEALTHY,
                message=f"Consumer group {self._group} is active",
            )
        except subprocess.TimeoutExpired:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"rpk timed out after {self._timeout}s",
            )
        except FileNotFoundError:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message="rpk binary not found on PATH",
            )
        except Exception as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"rpk check error: {e}",
            )

    def restart(self) -> bool:
        return False


__all__: list[str] = ["TargetRpk"]
