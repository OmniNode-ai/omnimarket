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

import re
import shlex
import subprocess
from collections.abc import Sequence
from typing import ClassVar, Protocol

from omnibase_spi.contracts.services import (
    ModelLaneChangeList,
    ModelLaneCloseoutCells,
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLaneHostStepResult,
    ModelLaneLedgerOutboxRows,
    ModelLanePrepareWorktreeRequest,
    ModelLanePrepareWorktreeResult,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
    ModelLaneReadLogTailRequest,
    ModelLaneReadLogTailResult,
    ModelLaneRegistryRootResult,
    ModelLaneRunDirsResult,
    ModelLaneShipPluginTreesRequest,
    ModelLaneShipPluginTreesResult,
    ModelLaneStagedInput,
    ModelLaneStageInputRequest,
    ModelLaneStartEngineRequest,
    ModelLaneStartEngineResult,
    ModelLaneStopEngineRequest,
    ModelLaneStopEngineResult,
)
from pydantic import BaseModel, ConfigDict

from omnimarket.models.engine_login import (
    LABEL_PATTERN,
    ModelEngineLoginDraw,
    ModelEngineLoginPool,
)

#: What every provisioning step of the protocol answers from this transport: the frozen runner
#: script still performs these host steps, so the node does not issue them yet (OMN-22848).
STEP_NOT_RUN_BY_NODE = "{op}: not run by the ssh transport; the frozen onex_remote_lane script still performs this step"

#: The exit code the wrapper never writes; read_engine_exit answers it for an engine whose whole
#: process group is gone with no exit file (killed with its wrapper by a signal to the group).
SIGNALLED_EXIT = 143


#: Exits that say the start script launched nothing: its own refusal before the launch (the run
#: directory could not be made) and the runner's exit for a host command that could not be run.
#: Any other end without a pid (ssh's 255, a timeout, a pid that never came back) may follow a launch.
START_REFUSED_EXIT = 3
HOST_COMMAND_UNAVAILABLE_EXIT = 127


#: Shell test on ``$pid``: the wrapper is alive, or any process of its group is. The group is what
#: stop_engine signals, so a wrapper that exited while an engine process survived still counts. The
#: wrapper is tested too, as it is not yet a group leader in the moment before its setsid.
_ALIVE = "{ kill -0 $pid 2>/dev/null || kill -0 -$pid 2>/dev/null; }"


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
            return ModelHostCommandResult(
                exit_code=HOST_COMMAND_UNAVAILABLE_EXIT, stderr=str(exc)
            )
        return ModelHostCommandResult(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


#: A PATH prefix is written inside double quotes in the host script, so it may carry only path
#: characters and ``$NAME`` references (``$HOME``); a quote, backtick, ``$(`` or space is refused.
_PATH_PREFIX = re.compile(r"(?:[A-Za-z0-9_./:-]|\$[A-Za-z_][A-Za-z0-9_]*)*")


#: The pool node's status line ends ``label=<label>`` for the token it chose; a refusal ends ``chosen=none``.
_DRAWN_LABEL = re.compile(r"(?:^|\s)label=(" + LABEL_PATTERN[1:-1] + r")\s*$")


def pool_python_sh(pool: ModelEngineLoginPool) -> str:
    """Shell that sets ``CPY`` to the first registry root's dispatch interpreter (empty when none), as the runner does."""
    tries = " ".join(shlex.quote(r.rstrip("/")) for r in pool.roots)
    return (
        f'CPY=""; for r in {tries}; do '
        f'if [ -x "$r/{pool.python_relative}" ]; then CPY="$r/{pool.python_relative}"; break; fi; done; '
    )


#: A literal path (a registry root, the marker directory, the run root) written into a host script unquoted.
_LITERAL_PATH = re.compile(r"[A-Za-z0-9_./-]+")

#: The runner script's exit for a host that holds no registry root, and for run directories that could not be made.
NO_REGISTRY_ROOT_EXIT = 96
RUN_DIRS_REFUSED_EXIT = 70


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
        path_prefix: str = "",
        login_pool: ModelEngineLoginPool | None = None,
        registry_root_candidates: Sequence[str] = (),
        registry_marker: str = "",
        runner: ProtocolHostCommandRunner | None = None,
    ) -> None:
        if not _PATH_PREFIX.fullmatch(path_prefix):
            raise ValueError(
                f"remote_lane_start.host_path_prefix {path_prefix!r}: only path characters and $NAME references"
            )
        self._path = f'export PATH="{path_prefix}:$PATH"; ' if path_prefix else ""
        if not host_run_root:
            raise ValueError("remote_lane_start.host_run_root is required")
        self._ssh_argv = tuple(ssh_argv)
        if not detach_argv:
            raise ValueError("remote_lane_start.detach_argv is required")
        self._root = host_run_root.rstrip("/")
        self._detach = " ".join(shlex.quote(a) for a in detach_argv)
        self._timeout = call_timeout_seconds
        self._pool = login_pool
        for candidate in registry_root_candidates:
            if not _LITERAL_PATH.fullmatch(candidate):
                raise ValueError(
                    f"remote_lane_start.registry_root_candidates {candidate!r}: only path characters"
                )
        if registry_marker and not _LITERAL_PATH.fullmatch(registry_marker):
            raise ValueError(
                f"remote_lane_start.registry_marker {registry_marker!r}: only path characters"
            )
        if not _LITERAL_PATH.fullmatch(self._root):
            raise ValueError(
                f"remote_lane_start.host_run_root {host_run_root!r}: only path characters"
            )
        self._candidates = tuple(registry_root_candidates)
        self._marker = registry_marker
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
            f"{self._path}mkdir -p {q(run)} || exit 3; for clone in {tries}; do "
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

    def draw_login(self, host: ModelLaneHostRef) -> ModelEngineLoginDraw:
        """Ask the host's token pool node which login the engine would run on; the answer is a label, never a value.

        The pool node's ``resolve`` writes the chosen token to stdout and its status line, with the label,
        to stderr. Here stdout goes to /dev/null on the host and only the last stderr line comes back, so
        no token crosses the ssh session or this process. The pool skips limited and revoked labels in its
        draw order; a pool that resolves nothing leaves the host login, as the runner's fallback does.
        """
        if self._pool is None:
            return ModelEngineLoginDraw(state="none", detail="no login pool configured")
        script = (
            f"{self._path}{pool_python_sh(self._pool)}"
            f'if [ -z "$CPY" ]; then echo "POOL-RC=none"; exit 0; fi; '
            f'line=$("$CPY" -m {shlex.quote(self._pool.module)} resolve 2>&1 >/dev/null); rc=$?; '
            f'echo "POOL-RC=$rc"; printf "%s\\n" "$line" | tail -n 1'
        )
        result = self._sh(host, script, self._pool.draw_timeout_seconds)
        lines = result.stdout.strip().splitlines()
        if result.exit_code != 0 or len(lines) < 2 or lines[-2] != "POOL-RC=0":
            return ModelEngineLoginDraw(
                state="none",
                detail=(lines[-2] if len(lines) >= 2 else f"exit {result.exit_code}")[
                    :120
                ],
            )
        found = _DRAWN_LABEL.search(lines[-1])
        if found is None:
            return ModelEngineLoginDraw(
                state="none", detail="no label in the pool line"
            )
        return ModelEngineLoginDraw(label=found.group(1), state="drawn")

    def start_engine_detached(
        self, request: ModelLaneStartEngineRequest
    ) -> ModelLaneStartEngineResult:
        run = self.run_dir(request.run_id)
        q = shlex.quote
        env = " ".join(q(f"{k}={v}") for k, v in request.environment)
        engine = " ".join(q(a) for a in request.engine_argv)
        # The engine's login is drawn by the pool node on the host, in the engine's own shell: the token
        # is exported there and appears in no command line, as the runner exports it.
        login = (
            "unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; "
            + pool_python_sh(self._pool)
            + f'if [ -n "$CPY" ] && CP=$("$CPY" -m {q(self._pool.module)} resolve 2>/dev/null); '
            'then export CLAUDE_CODE_OAUTH_TOKEN="$CP"; fi; unset CP; '
            if self._pool is not None
            else ""
        )
        inner = (
            f"{self._path}cd {q(request.workdir)} || exit 126; {login}"
            f"env {env} {engine} > {q(run + '/engine.log')} 2>&1 < /dev/null; "
            f"echo $? > {q(run + '/exit.tmp')}; mv {q(run + '/exit.tmp')} {q(run + '/exit')}"
        )
        # The wrapper is the session leader: its pid is the session and process-group id.
        script = (
            f"mkdir -p {q(request.workdir)} {q(run)} || exit {START_REFUSED_EXIT}; "
            f"{self._detach} sh -c {q(inner)} > /dev/null 2>&1 < /dev/null & echo $! > {q(run + '/pid')}; "
            f"cat {q(run + '/pid')}"
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        pid = result.stdout.strip().splitlines()[-1:] if result.stdout.strip() else []
        if result.exit_code == 0 and pid and pid[0].isdigit():
            return ModelLaneStartEngineResult(state="started", session_id=pid[0])
        if result.exit_code in (START_REFUSED_EXIT, HOST_COMMAND_UNAVAILABLE_EXIT):
            return ModelLaneStartEngineResult(
                state="failed",
                error=(result.stderr or f"start exit {result.exit_code}").strip()[
                    -500:
                ],
            )
        # The launch comes before the pid comes back: a lost answer may hide a running engine.
        return ModelLaneStartEngineResult(state="unresolved")

    def read_engine_exit(
        self, request: ModelLaneReadEngineExitRequest
    ) -> ModelLaneReadEngineExitResult:
        run = shlex.quote(self.run_dir(request.run_id))
        script = (
            f"if [ -f {run}/exit ]; then echo EXIT=$(cat {run}/exit); "
            f"elif pid=$(cat {run}/pid) && {_ALIVE}; then echo RUNNING; else echo GONE; fi"
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
            f"while {_ALIVE} && [ $i -lt {grace} ]; do sleep 1; i=$((i+1)); done; "
            f"if {_ALIVE}; then kill -KILL -$pid 2>/dev/null; sleep 1; fi; "
            f"if {_ALIVE}; then echo ALIVE; else echo GONE; fi"
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
        # tail counts newline-ended lines: only those are consumed, and the text after the last
        # newline (a record the engine is still writing) is read again by the next call.
        lines = tuple(result.stdout.split("\n")[:-1])
        return ModelLaneReadLogTailResult(
            lines=lines, next_offset=request.offset + len(lines)
        )

    def _not_run(self, op: str) -> str:
        return STEP_NOT_RUN_BY_NODE.format(op=op)

    def resolve_registry_root(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneRegistryRootResult:
        """The runner script's ``omni_home_script``: the first candidate that holds the marker directory.

        Same lines, same printed ``REMOTE-OMNI-HOME <path>`` or ``REMOTE-OMNI-HOME-NONE`` and exit 96.
        """
        if not self._candidates or not self._marker:
            return ModelLaneRegistryRootResult(
                error=self._not_run("resolve_registry_root")
            )
        tries = " ".join(shlex.quote(c) for c in self._candidates)
        script = "\n".join(
            [
                'H=""',
                f"for d in {tries}; do",
                f'  if [ -n "$d" ] && [ -d "$d/{self._marker}" ]; then H="$d"; break; fi',
                "done",
                f'if [ -z "$H" ]; then echo "REMOTE-OMNI-HOME-NONE $(hostname): none of {" ".join(self._candidates)} holds {self._marker}"; exit {NO_REGISTRY_ROOT_EXIT}; fi',
                'echo "REMOTE-OMNI-HOME $H"',
            ]
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        found = next(
            (
                line.split(" ", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.startswith("REMOTE-OMNI-HOME ")
            ),
            None,
        )
        if result.exit_code == 0 and found:
            return ModelLaneRegistryRootResult(registry_root=found)
        return ModelLaneRegistryRootResult(
            error=(
                result.stdout or result.stderr or f"exit {result.exit_code}"
            ).strip()[-300:]
        )

    def prepare_run_dirs(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneRunDirsResult:
        """The runner script's ``scratch_script``: the run directory and its ``.ctx`` beside it, made together."""
        run = self.run_dir(request.run_id)
        script = "\n".join(
            [
                f'W="{run}"; C="{run}.ctx"',
                f'mkdir -p "$W" "$C" || exit {RUN_DIRS_REFUSED_EXIT}',
                'echo "REMOTE-HOME $HOME"',
            ]
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        if result.exit_code == 0 and "REMOTE-HOME " in result.stdout:
            return ModelLaneRunDirsResult(run_dir=run, context_dir=f"{run}.ctx")
        return ModelLaneRunDirsResult(
            error=(
                result.stderr or result.stdout or f"exit {result.exit_code}"
            ).strip()[-300:]
            or f"exit {result.exit_code}"
        )

    def stage_ledger_snapshot(
        self, request: ModelLaneStageInputRequest
    ) -> ModelLaneStagedInput:
        return ModelLaneStagedInput(
            staged=False, error=self._not_run("stage_ledger_snapshot")
        )

    def stage_file(self, request: ModelLaneStageInputRequest) -> ModelLaneStagedInput:
        return ModelLaneStagedInput(staged=False, error=self._not_run("stage_file"))

    def ship_plugin_trees(
        self, request: ModelLaneShipPluginTreesRequest
    ) -> ModelLaneShipPluginTreesResult:
        return ModelLaneShipPluginTreesResult(
            shipped=False, error=self._not_run("ship_plugin_trees")
        )

    def mirror_ledger(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(ok=False, error=self._not_run("mirror_ledger"))

    def write_runtime_config(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(
            ok=False, error=self._not_run("write_runtime_config")
        )

    def drain_guard_spool(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(
            ok=False, error=self._not_run("drain_guard_spool")
        )

    def relay_reports(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(ok=False, error=self._not_run("relay_reports"))

    def read_ledger_outbox(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneLedgerOutboxRows:
        return ModelLaneLedgerOutboxRows(error=self._not_run("read_ledger_outbox"))

    def read_lane_changes(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneChangeList:
        return ModelLaneChangeList(error=self._not_run("read_lane_changes"))

    def closeout_worktrees(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneCloseoutCells:
        return ModelLaneCloseoutCells(error=self._not_run("closeout_worktrees"))

    def hold_run_dir_guard(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(
            ok=False, error=self._not_run("hold_run_dir_guard")
        )

    def retain_run_artifacts(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(
            ok=False, error=self._not_run("retain_run_artifacts")
        )

    def stop_lane_guard(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        return ModelLaneHostStepResult(ok=False, error=self._not_run("stop_lane_guard"))


__all__ = [
    "SIGNALLED_EXIT",
    "STEP_NOT_RUN_BY_NODE",
    "ModelHostCommandResult",
    "ProtocolHostCommandRunner",
    "SshLaneHostTransport",
    "SubprocessHostCommandExecutor",
    "pool_python_sh",
]
