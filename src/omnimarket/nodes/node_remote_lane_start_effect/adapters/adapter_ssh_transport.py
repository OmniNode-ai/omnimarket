# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real ProtocolLaneHostTransport: each call is one POSIX shell script run on the host (OMN-22848).

A remote host is reached through the overlay's ssh argv prefix (``remote_lane_start.ssh_argv``)
followed by the host's target; the caller's own host runs the script under ``sh -c``. The run's
files sit under ``<host_run_root>/<run_id>/``: ``engine.log`` (the engine's output), ``pid`` (the
session leader's pid, which is also its session and process-group id) and ``exit`` (written by the
wrapper when the engine ends). The engine starts under the overlay's detach prefix
(``remote_lane_start.detach_argv``: ``setsid`` on Linux, a ``setsid()`` exec shim where the host has
no such binary), so it is in a session of its own
and outlives the ssh session and the leader; ``stop_engine`` signals its whole process group.

No command in here carries a secret: the environment pairs are the overlay's names and values for
the engine and are visible in the host's process table, so a secret never goes through them.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from typing import ClassVar, Protocol

from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLanePrepareWorktreeRequest,
    ModelLanePrepareWorktreeResult,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
    ModelLaneReadLogTailRequest,
    ModelLaneReadLogTailResult,
    ModelLaneStartEngineRequest,
    ModelLaneStartEngineResult,
    ModelLaneStopEngineRequest,
    ModelLaneStopEngineResult,
)
from pydantic import BaseModel, ConfigDict

#: The exit code the wrapper never writes; read_engine_exit answers it for an engine that is
#: gone with no exit file (killed with its wrapper by a signal to the process group).
SIGNALLED_EXIT = 143


class ModelHostCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ProtocolHostCommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelHostCommandResult: ...


class SubprocessHostCommandExecutor:
    """Run an argv without a shell; a timeout is exit 124."""

    def run(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelHostCommandResult:
        try:
            done = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelHostCommandResult(exit_code=124, stderr="timed out")
        except OSError as exc:
            return ModelHostCommandResult(exit_code=127, stderr=str(exc))
        return ModelHostCommandResult(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


class SshLaneHostTransport:
    """Prepare, start, read and stop a lane's engine on a host with one shell script per call."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        ssh_argv: Sequence[str],
        host_run_root: str,
        call_timeout_seconds: float,
        detach_argv: Sequence[str],
        runner: ProtocolHostCommandRunner | None = None,
    ) -> None:
        if not host_run_root:
            raise ValueError("remote_lane_start.host_run_root is required")
        self._ssh_argv = tuple(ssh_argv)
        if not detach_argv:
            raise ValueError("remote_lane_start.detach_argv is required")
        self._root = host_run_root.rstrip("/")
        self._detach = " ".join(shlex.quote(a) for a in detach_argv)
        self._timeout = call_timeout_seconds
        self._runner = runner or SubprocessHostCommandExecutor()

    def run_dir(self, run_id: str) -> str:
        return f"{self._root}/{run_id}"

    def _argv(self, host: ModelLaneHostRef, script: str) -> list[str]:
        if host.local:
            return ["sh", "-c", script]
        if not self._ssh_argv:
            raise ValueError("remote_lane_start.ssh_argv is required for a remote host")
        return [*self._ssh_argv, host.target or host.name, script]

    def _sh(
        self, host: ModelLaneHostRef, script: str, timeout: float | None = None
    ) -> ModelHostCommandResult:
        return self._runner.run(self._argv(host, script), timeout or self._timeout)

    def prepare_worktree(
        self, request: ModelLanePrepareWorktreeRequest
    ) -> ModelLanePrepareWorktreeResult:
        run = self.run_dir(request.run_id)
        workdir = f"{run}/{request.repo}"
        q = shlex.quote
        tries = " ".join(
            q(f"{c.rstrip('/')}/{request.repo}")
            for c in request.registry_root_candidates
        )
        script = (
            f"mkdir -p {q(run)} || exit 3; for clone in {tries}; do "
            f'if [ -d "$clone/.git" ] || [ -f "$clone/.git" ]; then '
            f'git -C "$clone" worktree add --detach {q(workdir)} {q(request.sha)} >&2 || exit 4; '
            f'echo "ROOT=$(dirname "$clone")"; exit 0; fi; done; echo "no clone of {request.repo}" >&2; exit 5'
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        if result.exit_code != 0:
            return ModelLanePrepareWorktreeResult(
                prepared=False,
                error=(
                    result.stderr or result.stdout or f"exit {result.exit_code}"
                ).strip()[-500:],
            )
        root = next(
            (
                line[5:]
                for line in result.stdout.splitlines()
                if line.startswith("ROOT=")
            ),
            None,
        )
        return ModelLanePrepareWorktreeResult(
            prepared=True, registry_root=root, workdir=workdir
        )

    def start_engine_detached(
        self, request: ModelLaneStartEngineRequest
    ) -> ModelLaneStartEngineResult:
        run = self.run_dir(request.run_id)
        q = shlex.quote
        env = " ".join(q(f"{k}={v}") for k, v in request.environment)
        engine = " ".join(q(a) for a in request.engine_argv)
        inner = (
            f"cd {q(request.workdir)} || exit 126; "
            f"env {env} {engine} > {q(run + '/engine.log')} 2>&1 < /dev/null; "
            f"echo $? > {q(run + '/exit.tmp')}; mv {q(run + '/exit.tmp')} {q(run + '/exit')}"
        )
        # The wrapper is the session leader: its pid is the session and process-group id.
        script = (
            f"mkdir -p {q(request.workdir)} {q(run)} || exit 3; "
            f"{self._detach} sh -c {q(inner)} > /dev/null 2>&1 < /dev/null & echo $! > {q(run + '/pid')}; "
            f"cat {q(run + '/pid')}"
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        if result.exit_code == 124:
            return ModelLaneStartEngineResult(state="unresolved")
        pid = result.stdout.strip().splitlines()[-1:] if result.stdout.strip() else []
        if result.exit_code != 0 or not pid or not pid[0].isdigit():
            return ModelLaneStartEngineResult(
                state="failed",
                error=(result.stderr or f"start exit {result.exit_code}").strip()[
                    -500:
                ],
            )
        return ModelLaneStartEngineResult(state="started", session_id=pid[0])

    def read_engine_exit(
        self, request: ModelLaneReadEngineExitRequest
    ) -> ModelLaneReadEngineExitResult:
        run = shlex.quote(self.run_dir(request.run_id))
        script = (
            f"if [ -f {run}/exit ]; then echo EXIT=$(cat {run}/exit); "
            f"elif kill -0 $(cat {run}/pid) 2>/dev/null; then echo RUNNING; else echo GONE; fi"
        )
        out = self._sh(request.host, script).stdout.strip()
        if out.startswith("EXIT=") and out[5:].lstrip("-").isdigit():
            return ModelLaneReadEngineExitResult(running=False, exit_code=int(out[5:]))
        if out == "GONE":
            return ModelLaneReadEngineExitResult(
                running=False, exit_code=SIGNALLED_EXIT
            )
        return ModelLaneReadEngineExitResult(running=True)

    def stop_engine(
        self, request: ModelLaneStopEngineRequest
    ) -> ModelLaneStopEngineResult:
        run = shlex.quote(self.run_dir(request.run_id))
        grace = max(1, int(request.grace_seconds))
        script = (
            f"pid=$(cat {run}/pid) || exit 3; kill -TERM -$pid 2>/dev/null; i=0; "
            f"while kill -0 $pid 2>/dev/null && [ $i -lt {grace} ]; do sleep 1; i=$((i+1)); done; "
            f"if kill -0 $pid 2>/dev/null; then kill -KILL -$pid 2>/dev/null; sleep 1; fi; "
            f"if kill -0 $pid 2>/dev/null; then echo ALIVE; else echo GONE; fi"
        )
        result = self._sh(request.host, script, request.grace_seconds + self._timeout)
        confirmed = result.stdout.strip().endswith("GONE")
        return ModelLaneStopEngineResult(
            confirmed=confirmed,
            exit_code=SIGNALLED_EXIT if confirmed else None,
            detail=(result.stdout + result.stderr).strip()[-300:],
        )

    def read_log_tail(
        self, request: ModelLaneReadLogTailRequest
    ) -> ModelLaneReadLogTailResult:
        run = shlex.quote(self.run_dir(request.run_id))
        result = self._sh(
            request.host,
            f"tail -n +{request.offset + 1} {run}/engine.log 2>/dev/null || true",
        )
        lines = tuple(result.stdout.splitlines())
        return ModelLaneReadLogTailResult(
            lines=lines, next_offset=request.offset + len(lines)
        )


__all__ = [
    "SIGNALLED_EXIT",
    "ModelHostCommandResult",
    "ProtocolHostCommandRunner",
    "SshLaneHostTransport",
    "SubprocessHostCommandExecutor",
]
