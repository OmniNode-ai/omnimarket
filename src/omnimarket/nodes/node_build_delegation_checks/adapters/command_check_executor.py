# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs a check's probe entry point as the overlay declares it: exit 0 is pass, 1 is fail, anything else reads unavailable.

The probe sends its task through the delegation responder chain (local least busy, OpenRouter, Claude API); this executor names
no model and no address. A host that is down, a timeout, a missing route or a command that cannot start is unavailable, never
fail: a lab failure is not a regression (operator ruling 2026-11-23T16:21:00Z).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelCheckOutcome,
)


def _probe_name(argv: tuple[str, ...]) -> str:
    """The module a ``python -m <module>`` route runs, else the command's own name."""
    if "-m" in argv[:-1]:
        return argv[argv.index("-m") + 1]
    return argv[0]


class CommandCheckExecutor:
    """Placeholders in a route: ``{build_id}`` the build's sha, ``{python}`` the interpreter running this handler and
    ``{registry_root}`` the registry root the handler was built with and ``{state_root}`` the runtime's declared writable state
    root. The registry root is read-only in the runtime, so a probe's own state belongs under ``{state_root}``; a route that
    names it where no state root was declared reads unavailable, and no path is chosen."""

    def __init__(
        self,
        routes: Mapping[str, tuple[str, ...]],
        *,
        registry_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._routes = routes
        self._registry_root = str(registry_root) if registry_root is not None else ""
        self._state_root = str(state_root) if state_root is not None else ""

    def run(
        self, check_id: str, build_id: str, timeout_seconds: float
    ) -> ModelCheckOutcome:
        argv = self._routes.get(check_id)
        if not argv:
            return ModelCheckOutcome(
                reading=EnumCheckReading.UNAVAILABLE,
                detail="no probe route is declared for this check",
            )
        probe = _probe_name(argv)
        if not self._state_root and any("{state_root}" in arg for arg in argv):
            return ModelCheckOutcome(
                probe=probe,
                reading=EnumCheckReading.UNAVAILABLE,
                detail="the probe route names the state root and none is declared",
            )
        command = [
            arg.replace("{build_id}", build_id)
            .replace("{python}", sys.executable)
            .replace("{registry_root}", self._registry_root)
            .replace("{state_root}", self._state_root)
            for arg in argv
        ]
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelCheckOutcome(
                probe=probe,
                reading=EnumCheckReading.UNAVAILABLE,
                detail=f"the probe timed out after {timeout_seconds:g}s",
            )
        except OSError as exc:
            return ModelCheckOutcome(
                probe=probe,
                reading=EnumCheckReading.UNAVAILABLE,
                detail=f"the probe could not start: {type(exc).__name__}",
            )
        if done.returncode == 0:
            return ModelCheckOutcome(
                probe=probe, reading=EnumCheckReading.PASS, detail="the probe exited 0"
            )
        if done.returncode == 1:
            return ModelCheckOutcome(
                probe=probe, reading=EnumCheckReading.FAIL, detail="the probe exited 1"
            )
        return ModelCheckOutcome(
            probe=probe,
            reading=EnumCheckReading.UNAVAILABLE,
            detail=f"the probe could not measure the check: exit {done.returncode}",
        )


__all__ = ["CommandCheckExecutor"]
