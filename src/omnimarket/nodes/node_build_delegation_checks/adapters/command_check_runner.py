# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs a check's probe entry point as the overlay declares it: exit 0 is pass, 1 is fail, anything else reads unavailable.

The probe sends its task through the delegation responder chain (local least busy, OpenRouter, Claude API); this runner names
no model and no address. A host that is down, a timeout, a missing route or a command that cannot start is unavailable, never
fail: a lab failure is not a regression (operator ruling 2026-11-23T16:21:00Z).
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelCheckOutcome,
)


class CommandCheckRunner:
    def __init__(self, routes: Mapping[str, tuple[str, ...]]) -> None:
        self._routes = routes

    def run(
        self, check_id: str, build_id: str, timeout_seconds: float
    ) -> ModelCheckOutcome:
        argv = self._routes.get(check_id)
        if not argv:
            return ModelCheckOutcome(
                reading=EnumCheckReading.UNAVAILABLE,
                detail="no probe route is declared for this check",
            )
        command = [arg.replace("{build_id}", build_id) for arg in argv]
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
                reading=EnumCheckReading.UNAVAILABLE,
                detail=f"the probe timed out after {timeout_seconds:g}s",
            )
        except OSError as exc:
            return ModelCheckOutcome(
                reading=EnumCheckReading.UNAVAILABLE,
                detail=f"the probe could not start: {type(exc).__name__}",
            )
        if done.returncode == 0:
            return ModelCheckOutcome(
                reading=EnumCheckReading.PASS, detail="the probe exited 0"
            )
        if done.returncode == 1:
            return ModelCheckOutcome(
                reading=EnumCheckReading.FAIL, detail="the probe exited 1"
            )
        return ModelCheckOutcome(
            reading=EnumCheckReading.UNAVAILABLE,
            detail=f"the probe could not measure the check: exit {done.returncode}",
        )


__all__ = ["CommandCheckRunner"]
