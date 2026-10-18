# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger appender over the ledger's own append command (OMN-20807).

The dead-man runs on the host that holds the ledger, so it appends through the
ledger's validated writer (the argv it is given, with the row as the last
argument) and reads the ledger file back for the line the row landed on. A
refused append or a row that cannot be found afterwards is a failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelDeadmanEpisodeKey,
)

_STDERR_TAIL = 200


class LedgerCommandAppender:
    def __init__(
        self, ledger: Path, command: tuple[str, ...], *, timeout_seconds: int = 60
    ) -> None:
        self._ledger = ledger
        self._command = command
        self._timeout = timeout_seconds

    def find_line(self, key: ModelDeadmanEpisodeKey) -> str | None:
        needle_episode = f"| episode={key.episode_id} "
        needle_phase = f"| phase={key.phase.value} "
        try:
            lines = self._ledger.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        for number in range(len(lines), 0, -1):
            line = lines[number - 1]
            if needle_episode in line and needle_phase in line:
                return f"{self._ledger.name}:{number}"
        return None

    def append(self, key: ModelDeadmanEpisodeKey, row: str) -> str:
        try:
            proc = subprocess.run(
                [*self._command, row],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ledger append timed out after {self._timeout}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"ledger append could not start: {exc}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.strip()[-_STDERR_TAIL:]
            raise RuntimeError(f"ledger append returned {proc.returncode}: {tail}")
        line = self.find_line(key)
        if line is None:
            raise RuntimeError(
                "ledger append returned 0 but the row is not in the ledger"
            )
        return line


__all__ = ["LedgerCommandAppender"]
