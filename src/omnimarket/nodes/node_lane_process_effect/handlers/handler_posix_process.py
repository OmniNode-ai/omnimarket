# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The posix ProtocolLaneProcessRunner: start a process in a session of its own, or run one under a deadline.

``start_detached`` holds no handle on the process, signals nothing and waits for nothing: the process runs in
a session of its own with its output going to ``<output_dir>/<lane>.log``, and the call returns at once with the
pid. ``run_bounded`` runs the command in the foreground, returns its exit code and standard output, and at the
deadline ends the child's whole process group so no child is left running. The spec's environment is the
process's whole environment: nothing is inherited from the caller.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelLaneProcessRunResult,
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)

from omnimarket.nodes.node_lane_process_effect.overlay import ModelLaneProcessOverlay


class HandlerPosixProcess:
    """Starts processes on this host; the log directory is the overlay key floor_fill.output_dir."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, output_dir: str) -> None:
        if not output_dir:
            raise ValueError("no overlay supplies floor_fill.output_dir")
        self._output_dir = Path(output_dir)

    @classmethod
    def from_overlay(cls, overlay: ModelLaneProcessOverlay) -> HandlerPosixProcess:
        return cls(overlay.floor_fill.output_dir)

    def _log_path(self, lane: str) -> Path:
        return self._output_dir / f"{lane}.log"

    @staticmethod
    def _refusal(spec: ModelLaneStartSpec) -> str | None:
        """Why the spec cannot start, read before anything is created; None when it can."""
        if not Path(spec.working_directory).is_dir():
            return f"working directory missing: {spec.working_directory}"
        if Path(spec.lane).name != spec.lane or spec.lane in {".", ".."}:
            return f"lane is not a file name: {spec.lane}"
        env = dict(spec.environment)
        executable = spec.argv[0]
        if os.sep in executable:
            found = (
                executable
                if os.access(Path(spec.working_directory) / executable, os.X_OK)
                else None
            )
        else:
            found = shutil.which(executable, path=env.get("PATH", os.defpath))
        if found is None:
            return f"executable missing: {executable}"
        return None

    def start_detached(self, spec: ModelLaneStartSpec) -> ModelLaneProcessStartResult:
        refusal = self._refusal(spec)
        if refusal is not None:
            return ModelLaneProcessStartResult(state="failed", cause=refusal)
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            with self._log_path(spec.lane).open("ab") as log:
                process = subprocess.Popen(
                    spec.argv,
                    cwd=spec.working_directory,
                    env=dict(spec.environment),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as error:
            return ModelLaneProcessStartResult(
                state="failed", cause=f"start refused: {error}"
            )
        return ModelLaneProcessStartResult(state="started", pid=process.pid)

    def run_bounded(
        self, spec: ModelLaneStartSpec, timeout_seconds: float
    ) -> ModelLaneProcessRunResult:
        if timeout_seconds <= 0:
            return ModelLaneProcessRunResult(
                state="failed", cause="timeout_seconds must be positive"
            )
        refusal = self._refusal(spec)
        if refusal is not None:
            return ModelLaneProcessRunResult(state="failed", cause=refusal)
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            with self._log_path(spec.lane).open("ab") as log:
                process = subprocess.Popen(
                    spec.argv,
                    cwd=spec.working_directory,
                    env=dict(spec.environment),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    start_new_session=True,
                )
                try:
                    out, _ = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    if process.stdout is not None:
                        process.stdout.close()
                    return ModelLaneProcessRunResult(state="timed_out")
        except OSError as error:
            return ModelLaneProcessRunResult(
                state="failed", cause=f"run refused: {error}"
            )
        return ModelLaneProcessRunResult(
            state="exited",
            exit_code=process.returncode,
            stdout=out.decode("utf-8", errors="replace"),
        )
