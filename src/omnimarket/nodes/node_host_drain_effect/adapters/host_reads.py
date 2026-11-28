# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of the host itself: the overlay's argv, {host} replaced, prints one integer count for the read's kind."""

from __future__ import annotations

import subprocess

from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import CommandRun
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostReadSettings


class CommandHostRead:
    """Reads the host by the overlay's argv (no shell): the printed count, refused when it is not a non-negative integer."""

    def __init__(
        self, settings: ModelHostReadSettings, *, run: CommandRun = subprocess.run
    ) -> None:
        self.name = settings.name
        self.kind = settings.kind
        self._settings = settings
        self._run = run

    def read(self, host_name: str) -> int:
        argv = [part.replace("{host}", host_name) for part in self._settings.command]
        done = self._run(
            argv,
            capture_output=True,
            text=True,
            timeout=self._settings.timeout_seconds,
            check=True,
        )
        try:
            count = int(done.stdout.strip())
        except ValueError as exc:
            raise ValueError(
                f"the host read {self.name} printed a count that is not an integer: {done.stdout!r}"
            ) from exc
        if count < 0:
            raise ValueError(
                f"the host read {self.name} printed a negative count: {count}"
            )
        return count
