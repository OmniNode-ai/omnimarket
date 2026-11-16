# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start, list and stop a lane's engine inside the guard unit ``onexlane-<lane>-<id>`` (OMN-22192).

The bounded scope itself is the stdlib program ``runtime_lane_guard_host`` beside the contract (it ships to a lab
host as text, so it stays stdlib-only and Python 3.9 compatible). This handler is the node's face of it:

- ``start`` runs the command under ``lane_guard_host run`` in its own session and returns the guard's pid;
- ``list`` reads the process table and returns every process tagged with the unit (``ONEX_LANE_GUARD``);
- ``stop`` reaps the unit (the systemd scope when there is one, then every tagged process), re-reads the
  process table and answers ``confirmed_empty`` only when no tagged process is left. A process that
  started its own session (``setsid``) carries the tag, so it is found and killed too.
"""

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as host,
)
from omnimarket.nodes.node_lane_guard_unit_effect.models.model_lane_guard_unit import (
    EnumLaneGuardOperation,
    ModelLaneGuardMember,
    ModelLaneGuardOverlay,
    ModelLaneGuardUnitRequest,
    ModelLaneGuardUnitResult,
)

HOST_PROGRAM = Path(host.__file__)
CONFIRM_ATTEMPTS = 10
CONFIRM_INTERVAL_S = 0.3


NODE_NAME = "node_lane_guard_unit_effect"


def load_guard_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneGuardOverlay:
    """The end-record settings of the deployed guard, read from the node's overlay."""
    return load_structured_overlay(NODE_NAME, ModelLaneGuardOverlay, environ)


def host_program_source() -> str:
    """The guard program as text, the form a lab host receives it in."""
    return HOST_PROGRAM.read_text(encoding="utf-8")


class HandlerLaneGuardUnit:
    def handle(self, request: ModelLaneGuardUnitRequest) -> ModelLaneGuardUnitResult:
        unit = host.unit_name(request.lane, request.run_id)
        if request.operation is EnumLaneGuardOperation.START:
            return self._start(request, unit)
        if request.operation is EnumLaneGuardOperation.LIST:
            return ModelLaneGuardUnitResult(
                operation=request.operation, unit=unit, members=self._members(unit)
            )
        return self._stop(request, unit)

    def _refused(
        self, request: ModelLaneGuardUnitRequest, unit: str, why: str
    ) -> ModelLaneGuardUnitResult:
        return ModelLaneGuardUnitResult(
            operation=request.operation, unit=unit, refusal=why
        )

    def _start(
        self, request: ModelLaneGuardUnitRequest, unit: str
    ) -> ModelLaneGuardUnitResult:
        if not request.command:
            return self._refused(request, unit, "start needs a command")
        runtime_s = request.timeout_s + host.GRACE_S
        try:
            mem = (
                host.parse_size(request.mem_max)
                if request.mem_max
                else host.DEFAULT_MEM_BYTES
            )
            limits = host.Limits(
                runtime_s,
                mem,
                request.cpu_quota or host.DEFAULT_CPU_PCT,
                kind=request.kind,
            )
        except ValueError as exc:
            return self._refused(request, unit, str(exc))
        record_flags: list[str] = []
        if request.end_record is not None:
            record = request.end_record
            record_flags = [
                "--record-dir",
                record.record_dir,
                "--worktrees-dir",
                record.worktrees_dir,
                "--git-timeout-s",
                str(record.git_timeout_s),
                "--max-trees",
                str(record.max_trees),
            ]
        argv = [
            sys.executable,
            str(HOST_PROGRAM),
            "run",
            "--unit",
            unit,
            "--lane",
            request.lane,
            "--runtime-s",
            str(limits.runtime_s),
            "--mem-bytes",
            str(limits.mem_bytes),
            "--cpu-pct",
            str(limits.cpu_pct),
            "--tasks-max",
            str(limits.tasks_max),
            "--kind",
            limits.kind,
            *record_flags,
            "--",
            *request.command,
        ]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return ModelLaneGuardUnitResult(
            operation=request.operation, unit=unit, started_pid=proc.pid
        )

    def _members(self, unit: str) -> tuple[ModelLaneGuardMember, ...]:
        me = os.getpid()
        return tuple(
            ModelLaneGuardMember(
                pid=p.pid, age_s=int(p.age_s), rss_kb=p.rss_kb, cmd=p.cmd[:160]
            )
            for p in host.read_proc_table()
            if p.tag == unit and p.pid != me
        )

    def _stop(
        self, request: ModelLaneGuardUnitRequest, unit: str
    ) -> ModelLaneGuardUnitResult:
        reaped = host.reap_unit(unit)
        killed = reaped["killed"]
        killed_members = tuple(
            ModelLaneGuardMember(
                pid=int(k["pid"]),
                age_s=int(k["age_s"]),
                rss_kb=int(k["rss_kb"]),
                cmd=str(k["cmd"]),
            )
            for k in (killed if isinstance(killed, list) else [])
        )
        members = self._members(unit)
        for _ in range(CONFIRM_ATTEMPTS):
            if not members:
                break
            time.sleep(CONFIRM_INTERVAL_S)
            members = self._members(unit)
        return ModelLaneGuardUnitResult(
            operation=request.operation,
            unit=unit,
            members=members,
            killed=killed_members,
            systemd_stopped=bool(reaped["systemd_stopped"]),
            confirmed_empty=not members,
        )
