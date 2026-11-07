# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real storage backend: an argv list run as a subprocess, behind the host's transport prefix.

No shell is involved: the overlay's words are passed as a list. The free-bytes reading is the
overlay's own argv, run the same way.
"""

from __future__ import annotations

import subprocess

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelStorageCommand,
    ModelStorageHost,
)

_TAIL_CHARS = 400


class BackendHostStorageSubprocess:
    def __init__(
        self, free_bytes_argv: tuple[str, ...], free_bytes_timeout_seconds: float
    ) -> None:
        self._free_argv = free_bytes_argv
        self._free_timeout = free_bytes_timeout_seconds

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        argv = [
            *host.transport_prefix,
            *(w.replace("{path}", path) for w in self._free_argv),
        ]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._free_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0:
            return None
        lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        first = lines[-1].split()[0]
        return int(first) if first.isdigit() else None

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun:
        argv = [*host.transport_prefix, *command.argv]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelHostStorageRun(exit_code=None, timed_out=True)
        except OSError as exc:
            return ModelHostStorageRun(exit_code=None, output_tail=type(exc).__name__)
        tail = (done.stdout + done.stderr)[-_TAIL_CHARS:]
        return ModelHostStorageRun(exit_code=done.returncode, output_tail=tail)
