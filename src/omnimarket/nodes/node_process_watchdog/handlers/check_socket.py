"""SocketCheckTarget — real Unix socket file health check.

Replaces the Phase-1 mock per OMN-9581 Phase 2.
Uses os.stat() + stat.S_ISSOCK() — no subprocess, no network calls.

Status mapping:
  DOWN     — path missing or not a socket
  DEGRADED — socket exists but mtime older than stale_after_seconds
  HEALTHY  — socket exists, is a socket, mtime within threshold
"""

from __future__ import annotations

import os
import stat
import time

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)


class SocketCheckTarget:
    """Check a Unix domain socket file via stat()."""

    def __init__(self, socket_path: str, stale_after_seconds: int = 300) -> None:
        self._socket_path = socket_path
        self._stale_after_seconds = stale_after_seconds

    @property
    def name(self) -> str:
        return f"unix_socket:{self._socket_path}"

    @property
    def category(self) -> EnumCheckTarget:
        return EnumCheckTarget.UNIX_SOCKET

    def check(self) -> ModelWatchdogCheckResult:
        try:
            st = os.stat(self._socket_path)
        except FileNotFoundError:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"Socket does not exist: {self._socket_path}",
            )
        except (OSError, ValueError) as e:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"Cannot stat socket: {e}",
            )

        if not stat.S_ISSOCK(st.st_mode):
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DOWN,
                message=f"Path exists but is not a socket: {self._socket_path}",
            )

        age_seconds = time.time() - st.st_mtime
        if age_seconds > self._stale_after_seconds:
            return ModelWatchdogCheckResult(
                target=self.name,
                category=self.category,
                status=EnumCheckStatus.DEGRADED,
                message=(
                    f"Socket is stale: mtime {age_seconds:.0f}s ago "
                    f"(threshold {self._stale_after_seconds}s)"
                ),
                details={
                    "age_seconds": age_seconds,
                    "threshold": self._stale_after_seconds,
                },
            )

        return ModelWatchdogCheckResult(
            target=self.name,
            category=self.category,
            status=EnumCheckStatus.HEALTHY,
            message=f"Socket healthy: {self._socket_path}",
            details={"age_seconds": age_seconds},
        )

    def restart(self) -> bool:
        return False


__all__: list[str] = ["SocketCheckTarget"]
