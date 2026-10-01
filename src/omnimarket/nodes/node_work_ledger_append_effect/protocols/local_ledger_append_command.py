# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger host's append command and file reader (OMN-20275)."""

import os
import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelAppendCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ProtocolLedgerAppendRunner(Protocol):
    def append(self, rows: str) -> ModelAppendCommandResult: ...


class ProtocolLedgerReader(Protocol):
    def read_text(self) -> str: ...


class LocalLedgerAppendCommand:
    """Invoke the operator-supplied argv prefix without a shell."""

    # The append command waits this long for the ledger lock and exits 75 when it
    # cannot take it, so the process is never killed mid-write by our own timeout.
    LOCK_WAIT_S = 60

    def __init__(self, argv_prefix: list[str], timeout_s: float = 150.0) -> None:
        if not argv_prefix:
            raise ValueError("append command must not be empty")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._argv_prefix = list(argv_prefix)
        self._timeout_s = timeout_s

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
                # A request must never be re-published to the bus it came from.
                env={**os.environ, "ONEX_LEDGER_WRITE_VIA": "local"},
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
