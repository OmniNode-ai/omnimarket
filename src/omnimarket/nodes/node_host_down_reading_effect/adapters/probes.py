# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real ports of the host-down reading: a TCP probe and a command advertisement source."""

from __future__ import annotations

import socket
import subprocess
from datetime import datetime
from typing import Any


class TcpHostProber:
    """A host is up when a TCP connection to its port succeeds within the timeout."""

    def probe(
        self, host_name: str, address: str, port: int, timeout_s: float
    ) -> bool | None:
        try:
            with socket.create_connection((address, port), timeout=timeout_s):
                return True
        except OSError:
            return False


class CommandAdvertisementSource:
    """Runs the overlay's argv without a shell and reads the newest advertisement per host.

    The command prints one line per host that advertised capacity: ``<host> <ISO-8601 time>``.
    A failed command or a timeout raises OSError.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        timeout_s: int,
        *,
        run: Any = subprocess.run,
    ) -> None:
        self._argv = argv
        self._timeout_s = timeout_s
        self._run = run

    def newest(self, host_name: str) -> datetime | None:
        try:
            done = self._run(
                list(self._argv),
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=True,
            )
            for line in done.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == host_name:
                    return datetime.fromisoformat(parts[1])
        except (subprocess.SubprocessError, ValueError) as exc:
            raise OSError(
                f"the advertisement command failed: {type(exc).__name__}"
            ) from exc
        return None
