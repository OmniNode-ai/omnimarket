# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the installed node through a host overlay's declared execution command."""

from __future__ import annotations

import subprocess

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
)

from ..models.model_quiet_host_repair import ModelHostDrainerCommand
from ..models.model_silent_failure_alarm_result import ModelSilentFailureAlarmResult


class HandlerCommandDrainerWatch:
    def __init__(self, host: str, target: ModelHostDrainerCommand) -> None:
        self._host, self._target = host, target

    def _call(self, operation: str) -> ModelSilentFailureAlarmResult:
        done = subprocess.run(
            [*self._target.command, operation],
            capture_output=True,
            text=True,
            check=False,
            timeout=self._target.timeout_seconds,
        )
        if done.returncode != 0:
            raise ValueError(
                f"{self._host}: node {operation} exited {done.returncode}: {done.stderr[-200:]}"
            )
        result = ModelSilentFailureAlarmResult.model_validate_json(done.stdout)
        if result.host != self._host or result.exit_code != 0:
            raise ValueError(
                f"{self._host}: node returned another host or refused the operation"
            )
        return result

    def observe(self) -> ModelDrainerObservation:
        result = self._call("drainer-observe")
        if result.observation is None:
            raise ValueError(f"{self._host}: node returned no observation")
        return result.observation

    def repair(self) -> ModelDrainerRepair:
        result = self._call("drainer-repair")
        if result.repair is None:
            raise ValueError(f"{self._host}: node returned no installer receipt")
        return result.repair
