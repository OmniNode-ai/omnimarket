# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the lanes a host is running from its systemd user scopes (OMN-24489).

The one place the lab work unit node reads the host's lanes. It runs the
contract's ``capacity.lane_scope_command`` (no shell) and pulls the lane name
out of each active scope's unit name with the contract's
``capacity.lane_scope_pattern``. A failed read -- a non-zero exit, an OSError
or a subprocess error -- returns None, never an empty list: an unread lane
count is not a measured zero, and the advertisement then carries no
``lanes-read:ok`` tool so a caller on an older omnimarket still parses it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class LocalLaneReader:
    """Reads the lanes this host is running from its systemd user scopes."""

    def __init__(
        self,
        command: Sequence[str],
        pattern: str,
        timeout_seconds: float,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._command = list(command)
        self._pattern = re.compile(pattern)
        self._timeout_seconds = timeout_seconds
        self._run = run

    def read(self) -> list[str] | None:
        """The sorted lane names this host is running, or None when the read failed (a failed read is never an empty list)."""
        try:
            done = self._run(
                self._command,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        lanes: list[str] = []
        for line in done.stdout.splitlines():
            word = line.split()[0] if line.split() else ""
            match = self._pattern.match(word)
            if match is not None:
                lanes.append(match.group("lane"))
        return sorted(lanes)


def load_lane_reader() -> LocalLaneReader:
    """The lane reader this host's contract declares: its ``capacity.lane_scope_command``, ``lane_scope_pattern`` and ``lane_scope_timeout_seconds``."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text())["capacity"]
    return LocalLaneReader(
        contract["lane_scope_command"],
        contract["lane_scope_pattern"],
        contract["lane_scope_timeout_seconds"],
    )


__all__ = [
    "CONTRACT_PATH",
    "LocalLaneReader",
    "load_lane_reader",
]
