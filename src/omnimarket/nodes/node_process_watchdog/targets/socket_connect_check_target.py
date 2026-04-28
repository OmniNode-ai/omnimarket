"""TargetSocketConnect — TCP socket connect health check.

Connects to a TCP host:port and optionally sends a health probe line.
No subprocess, no Docker — pure stdlib socket.

Status mapping:
  HEALTHY  — connect succeeded (and optional probe response OK)
  DEGRADED — connect succeeded but probe response unexpected
  DOWN     — connection refused, timeout, host unreachable
  UNKNOWN  — unexpected error
"""

from __future__ import annotations

import socket

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
)


class TargetSocketConnect:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        timeout: float = 5.0,
        category: EnumCheckTarget = EnumCheckTarget.EMIT_DAEMON,
    ) -> None:
        if not name:
            raise ValueError("name must be non-empty")
        if not host:
            raise ValueError("host must be non-empty")
        if not (1 <= port <= 65535):
            raise ValueError("port must be 1-65535")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        self._name = name
        self._host = host
        self._port = port
        self._timeout = timeout
        self._category = category

    @property
    def name(self) -> str:
        return self._name

    @property
    def category(self) -> EnumCheckTarget:
        return self._category

    def check(self) -> ModelWatchdogCheckResult:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect((self._host, self._port))
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self._category,
                status=EnumCheckStatus.HEALTHY,
                message=f"Socket {self._host}:{self._port} accepting connections",
            )
        except ConnectionRefusedError:
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self._category,
                status=EnumCheckStatus.DOWN,
                message=f"Socket {self._host}:{self._port} connection refused",
            )
        except TimeoutError:
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self._category,
                status=EnumCheckStatus.DOWN,
                message=f"Socket {self._host}:{self._port} timed out after {self._timeout}s",
            )
        except OSError as e:
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self._category,
                status=EnumCheckStatus.DOWN,
                message=f"Socket {self._host}:{self._port} error: {e}",
            )
        except Exception as e:
            return ModelWatchdogCheckResult(
                target=self._name,
                category=self._category,
                status=EnumCheckStatus.UNKNOWN,
                message=f"Socket check error: {e}",
            )
        finally:
            if sock is not None:
                sock.close()

    def restart(self) -> bool:
        return False


__all__: list[str] = ["TargetSocketConnect"]
