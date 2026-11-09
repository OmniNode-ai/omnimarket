# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker's log lines that hold a needle: from given lines (the in-memory default) or over ssh.

The ssh backend asks the broker host for its container's log with ``docker logs --since <n>s -t`` and
keeps only the lines that hold the needle. Nothing else of the log leaves this module.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence

from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    ModelBrokerLogSource,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.protocols import (
    BrokerLogError,
)

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


class InMemoryBrokerLog:
    """The broker's log says given lines, or nothing (an unreadable read)."""

    def __init__(self, lines: Sequence[str] | None) -> None:
        self.lines = None if lines is None else tuple(lines)

    def matching_lines(self, needle: str, window_seconds: int) -> tuple[str, ...]:
        if self.lines is None:
            raise BrokerLogError("log-unreadable")
        return tuple(line for line in self.lines if needle in line)


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this adapter; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


class SshBrokerLog:
    """Read the broker container's log over ssh and keep the lines that hold the needle."""

    def __init__(
        self,
        source: ModelBrokerLogSource,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._source = source
        self._environ = environ
        self._run = run

    def _script(self, needle: str, window_seconds: int) -> str:
        source = self._source
        return "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={source.compose_project} --filter "
                f"label=com.docker.compose.service={source.service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'OUT=$(docker logs --since {int(window_seconds)}s -t "$RT" 2>&1)',
                f"printf '%s\\n' \"$OUT\" | grep -F -- {shlex.quote(needle)} || true",
            ]
        )

    def matching_lines(self, needle: str, window_seconds: int) -> tuple[str, ...]:
        source = self._source
        host = self._environ.get(source.ssh_host_env, "").strip()
        if not host:
            raise BrokerLogError("ssh-host-variable-unset")
        try:
            done = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                self._script(needle, window_seconds).encode(),
                source.read_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise BrokerLogError("log-read-timeout") from None
        except OSError as exc:
            raise BrokerLogError(f"log-read-unstartable-{type(exc).__name__}") from None
        if done.returncode:
            raise BrokerLogError(f"log-read-exit-{done.returncode}")
        text = done.stdout.decode("utf-8", errors="replace")
        return tuple(line for line in text.splitlines() if line.strip())


__all__ = ["InMemoryBrokerLog", "SshBrokerLog", "run_process"]
