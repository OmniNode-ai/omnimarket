# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger host's append command and file reader (OMN-20275)."""

import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

WRITE_VIA_MODES = ("local", "bus")


class ModelAppendCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ProtocolLedgerAppendRunner(Protocol):
    def append(self, rows: str) -> ModelAppendCommandResult: ...


class ProtocolLedgerReader(Protocol):
    def read_text(self) -> str: ...


class ProtocolLedgerAppendLock(Protocol):
    """Mutual exclusion around one check-then-append step (OMN-22185)."""

    def hold(self) -> AbstractContextManager[None]: ...


class LedgerLockTimeoutError(TimeoutError):
    """The append lock was not free within its declared wait."""


class InMemoryLedgerAppendLock:
    """In-process adapter: threads of one process exclude each other."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            yield


class LocalLedgerAppendLock:
    """flock on a lock file beside the ledger: processes and threads exclude each other.

    Each hold opens its own descriptor, so two holds in one process conflict as two
    processes do. The wait is bounded; the lock file stays, empty, between holds.
    """

    _POLL_S = 0.005

    def __init__(self, path: Path, wait_s: float) -> None:
        if wait_s <= 0:
            raise ValueError("wait_s must be positive")
        self._path = path
        self._wait_s = wait_s

    @contextmanager
    def hold(self) -> Iterator[None]:
        deadline = time.monotonic() + self._wait_s
        with self._path.open("a+") as stream:
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LedgerLockTimeoutError(
                            f"ledger append lock {self._path} not free within {self._wait_s}s"
                        ) from None
                    time.sleep(self._POLL_S)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class LocalLedgerAppendCommand:
    """Invoke the operator-supplied argv prefix without a shell."""

    # The append command waits this long for the ledger lock and exits 75 when it
    # cannot take it, so the process is never killed mid-write by our own timeout.
    LOCK_WAIT_S = 60

    def __init__(
        self,
        argv_prefix: list[str],
        timeout_s: float = 150.0,
        *,
        write_via: str = "local",
    ) -> None:
        if not argv_prefix:
            raise ValueError("append command must not be empty")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if write_via not in WRITE_VIA_MODES:
            raise ValueError(f"write_via must be one of {WRITE_VIA_MODES}")
        self._argv_prefix = list(argv_prefix)
        self._timeout_s = timeout_s
        self._write_via = write_via

    def append(self, rows: str) -> ModelAppendCommandResult:
        try:
            result = subprocess.run(
                [
                    *self._argv_prefix,
                    "--append",
                    rows,
                    "--timeout",
                    str(self.LOCK_WAIT_S),
                ],
                stdin=subprocess.DEVNULL,
                # The ledger serve keeps "local": a request must never be
                # re-published to the bus it came from. A writer that is not the
                # serve (the lane start leader, OMN-22848) passes "bus", because a
                # host whose ledger of record is the bus refuses a direct file
                # append from anyone but the serve.
                env={**os.environ, "ONEX_LEDGER_WRITE_VIA": self._write_via},
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ModelAppendCommandResult(
                exit_code=124,
                stdout=_timeout_text(exc.stdout),
                stderr=_timeout_text(exc.stderr)
                or f"append command timed out after {self._timeout_s}s",
            )
        except OSError as exc:
            return ModelAppendCommandResult(exit_code=70, stderr=str(exc))
        return ModelAppendCommandResult(
            exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


class LocalLedgerFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read_text(self) -> str:
        return self._path.read_text(encoding="utf-8")
