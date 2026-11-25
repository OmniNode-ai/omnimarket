# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host adapters: the sensors (CPU package and nvidia-smi GPUs) and the lane cutter (the guard's stop level)."""

from __future__ import annotations

import fnmatch
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_host_thermal_observe_effect.handlers.handler_host_thermal_observe import (
    read_cpu_temp,
    read_gpus,
)

from ..models import ModelThermalReading

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class HostThermalSensors:
    """The CPU package temperature the way placement reads it (two reads one second apart) and every GPU nvidia-smi lists."""

    def __init__(
        self,
        hwmon_root: Path,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._hwmon = hwmon_root
        self._run = run
        self._sleep = sleep

    def read(self) -> ModelThermalReading:
        first = read_cpu_temp(self._hwmon)
        self._sleep(1.0)
        second = read_cpu_temp(self._hwmon)
        values = [v for v in (first, second) if v is not None]
        cpu = sum(values) / len(values) if values else None
        gpus, _ = read_gpus(self._run)
        return ModelThermalReading(
            datetime.now(UTC).strftime(_STAMP), cpu, tuple(g.temp_c for g in gpus)
        )


class HostLaneCutter:
    """Stops the systemd user scopes of the remote lanes and the processes carrying the lane guard's tag, by pid.

    Never a name match on a command line, never a model server, docker, the deploy agent or the Actions runner: none carries the tag
    or lives in a lane scope."""

    def __init__(
        self,
        scope_pattern: str,
        tag_env: str,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        proc_root: Path = Path("/proc"),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pattern = scope_pattern
        self._tag = f"{tag_env}=".encode()
        self._run = run
        self._proc = proc_root
        self._sleep = sleep

    def _scopes(self) -> list[str]:
        done = self._run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--all",
                "--type=scope",
                "--no-legend",
                "--plain",
                self._pattern,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return [
            line.split()[0]
            for line in done.stdout.splitlines()
            if line.strip()
            and fnmatch.fnmatch(line.split()[0], self._pattern + ".scope")
        ]

    def _tagged(self) -> list[int]:
        me, uid = os.getpid(), os.getuid()
        pids: list[int] = []
        for entry in self._proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if self._tag in environ:
                pids.append(int(entry.name))
        return pids

    def cut(self) -> tuple[str, ...]:
        stopped: list[str] = []
        for scope in self._scopes():
            self._run(
                ["systemctl", "--user", "stop", scope],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            stopped.append(scope)
        self._sleep(2.0)
        killed: list[int] = []
        for pid in self._tagged():
            try:
                os.kill(pid, 15)
            except OSError:
                continue
            killed.append(pid)
        if killed:
            self._sleep(5.0)
            for pid in killed:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)
                except OSError:
                    continue
        return (*stopped, *(f"pid:{pid}" for pid in killed))
