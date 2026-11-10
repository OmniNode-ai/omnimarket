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

import base64
import binascii
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
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
    ModelEngineLoginSteer,
)
from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    CHANGES_LINE_RE,
    changes_script,
    closeout_script,
    closeout_unavailable,
    parse_closeout,
)
from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    hold_run_dir_guard as _hold_run_dir_guard,
)
from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    remote_script as relay_remote_script,
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
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult: ...


class SubprocessHostCommandExecutor:
    """Run an argv without a shell; a timeout is exit 124. ``stdin`` bytes (a staged file) go to the command's stdin."""

    def run(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        try:
            done = subprocess.run(
                list(argv),
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
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
            exit_code=done.returncode,
            stdout=done.stdout.decode("utf-8", errors="replace"),
            stderr=done.stderr.decode("utf-8", errors="replace"),
        )


#: A PATH prefix is written inside double quotes in the host script, so it may carry only path
#: characters and ``$NAME`` references (``$HOME``); a quote, backtick, ``$(`` or space is refused.
_PATH_PREFIX = re.compile(r"(?:[A-Za-z0-9_./:-]|\$[A-Za-z_][A-Za-z0-9_]*)*")


#: The pool node's status line ends ``label=<label>`` for the token it chose; a refusal ends ``chosen=none``.
_DRAWN_LABEL = re.compile(r"(?:^|\s)label=(" + LABEL_PATTERN[1:-1] + r")\s*$")


#: The pool node's exit when it holds a usable token and refuses the start, and the code its line then ends with.
POOL_START_REFUSED_EXIT = 5
_REFUSED_START = re.compile(r"(?:^|\s)refusal=([a-z_]+)\s*$")


def pool_python_sh(pool: ModelEngineLoginPool) -> str:
    """Shell that sets ``CPY`` to the first registry root's dispatch interpreter (empty when none), as the runner does."""
    tries = " ".join(shlex.quote(r.rstrip("/")) for r in pool.roots)
    return (
        f'CPY=""; for r in {tries}; do '
        f'if [ -x "$r/{pool.python_relative}" ]; then CPY="$r/{pool.python_relative}"; break; fi; done; '
    )


#: A literal path (a registry root, the marker directory, the run root) written into a host script unquoted.
_LITERAL_PATH = re.compile(r"[A-Za-z0-9_./-]+")

#: A path under the registry root, written unquoted: it starts with a path character, never a slash, and has no ``..`` hop past the root's parent.
_RELATIVE_PATH = re.compile(r"(?:\.\./)?[A-Za-z0-9_][A-Za-z0-9_./-]*")

#: The runner script's exit for a host that holds no registry root, and for run directories that could not be made.
NO_REGISTRY_ROOT_EXIT = 96
RUN_DIRS_REFUSED_EXIT = 70

#: A file name inside the context directory: one path component, no separator, no leading dot or dash.
_STAGED_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

#: The runner's credential screen over the launching host's runtime config (``RUNTIME_CONFIG_SECRET_RE``).
RUNTIME_CONFIG_SECRET = re.compile(
    r"password|passwd|secret|token|api[_-]?key|sasl_", re.IGNORECASE
)
#: The file mode ``put_file`` leaves (``mktemp`` makes the file 0600); only another mode adds a ``chmod``.
STAGED_DEFAULT_MODE = 0o600
SNAPSHOT_PRINTED = "LEDGER-SNAPSHOT "

#: The staged file name the mirror script reads the lane's CLAIM row from (the spi request carries no row).
CLAIM_ROW_FILE = "claim-row.txt"
#: The lines the runner's ``ledger_mirror_script`` prints.
MIRROR_PRINTED = "LEDGER-MIRROR "
MIRROR_PRINTED_OK = "LEDGER-MIRROR ok"
MIRROR_PRINTED_FAILED = "LEDGER-MIRROR failed"
RUNTIME_CONFIG_PRINTED = ("RUNTIME-CONFIG installed ", "RUNTIME-CONFIG present ")

#: The first-line prefix the runner's closeout script opens with: the home-relative run root, swapped here for the declared one.
CLOSEOUT_HEADER_PREFIX = 'LR="$HOME/lab-run"; C="$LR/runs/{run_id}.ctx"; '

#: A plugin store path written inside double quotes: path characters, optionally under ``$HOME``.
_PLUGIN_STORE = re.compile(r"(?:\$HOME)?[A-Za-z0-9_./-]+")
#: A plugin name as the engine loads it, and the digest that names the cached tree.
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_TREE_DIGEST = re.compile(r"[A-Za-z0-9]{12,}")
PLUGIN_DIR_PRINTED = "PLUGIN-DIR "
#: The runner builds a shared virtualenv for a tree that carries this lock file; the ssh transport does not yet.
PLUGIN_LOCK_FILE = "uv.lock"
PLUGIN_VENV_NOT_RUN = "PLUGIN-VENV-NOT-RUN "
PLUGIN_VENV_EXIT = 21

#: The ledger outbox file under the run's context directory, read after the engine ends.
OUTBOX_FILE = "ledger-outbox.rows"

#: The line the runner's guarded cleanup header ends with when no lane tree keeps a git dir inside the run directory.
RETENTION_SAFE_LINE = "LANE-RETENTION-SAFE"
# The runner's lane_guard (omni plugin scripts/lane_guard.py): the host program rides in a shell variable, the
# command after it reaps a unit or drains the host reaper's queued records. Prefix, variable and printed lines
# are the runner's; the program itself is injected (``guard_host_source``), never read from a path here.
GUARD_PATH_LINE = (
    'export PATH="${ONEX_LANE_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/omni/lane-bin}'
    ':$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"'
)
GUARD_SOURCE_VARIABLE = "G_SRC"
GUARD_UNIT_PREFIX = "onexlane-"
GUARD_STOP_PREFIX = "LANE-GUARD-STOP "
GUARD_SPOOL_PREFIX = "LANE-GUARD-SPOOL "
GUARD_SOURCE_MISSING = (
    "{op}: no guard host program was given to the ssh transport (guard_host_source)"
)


def _guard_sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", text)


def guard_unit_name(lane: str, run_id: str) -> str:
    """The runner's ``lane_guard.unit_name``: ``onexlane-<lane>-<tail of the run id>``."""
    tail = _guard_sanitize(run_id).rsplit("-", 1)[-1][-12:].strip("-.") or "run"
    return f"{GUARD_UNIT_PREFIX}{_guard_sanitize(lane)[:60].strip('-.')}-{tail}"


def guard_script(source: str, command: str) -> str:
    """The runner's ``reap_script`` / ``drain_script`` text: path line, program in a variable, one guard command."""
    return "\n".join(
        [
            GUARD_PATH_LINE,
            f"{GUARD_SOURCE_VARIABLE}={shlex.quote(source)}",
            f'python3 -c "${GUARD_SOURCE_VARIABLE}" {command}',
        ]
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
        path_prefix: str = "",
        login_pool: ModelEngineLoginPool | None = None,
        login_steer: Callable[[], ModelEngineLoginSteer | None] | None = None,
        registry_root_candidates: Sequence[str] = (),
        registry_marker: str = "",
        guard_host_source: str = "",
        ledger_write_via: str = "local",
        runtime_config_text: str = "",
        runtime_config_target: str = "",
        closeout_tool_repo: str = "",
        closeout_ledger_env: str = "",
        closeout_registry_env: str = "",
        plugin_store: str = "",
        runner: ProtocolHostCommandRunner | None = None,
    ) -> None:
        if ledger_write_via not in ("local", "bus"):
            raise ValueError(
                f"remote_lane_start.ledger_write_via {ledger_write_via!r}: local or bus"
            )
        hit = RUNTIME_CONFIG_SECRET.search(runtime_config_text)
        if hit:
            raise ValueError(
                f"remote_lane_start.runtime_config_text carries credential-like text ({hit.group(0)!r}); refusing to ship it"
            )
        self._ledger_via = ledger_write_via
        if runtime_config_target and not _RELATIVE_PATH.fullmatch(
            runtime_config_target
        ):
            raise ValueError(
                f"remote_lane_start.runtime_config_target {runtime_config_target!r}: a relative path of path characters"
            )
        if closeout_tool_repo and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*", closeout_tool_repo
        ):
            raise ValueError(
                f"remote_lane_start.closeout_tool_repo {closeout_tool_repo!r}: a plain repository directory name"
            )
        if closeout_ledger_env and not re.fullmatch(
            r"[A-Z][A-Z0-9_]*", closeout_ledger_env
        ):
            raise ValueError(
                f"remote_lane_start.closeout_ledger_env {closeout_ledger_env!r}: an upper-case variable name"
            )
        if closeout_registry_env and not re.fullmatch(
            r"[A-Z][A-Z0-9_]*", closeout_registry_env
        ):
            raise ValueError(
                f"remote_lane_start.closeout_registry_env {closeout_registry_env!r}: an upper-case variable name"
            )
        if plugin_store and not _PLUGIN_STORE.fullmatch(plugin_store):
            raise ValueError(
                f"remote_lane_start.plugin_store {plugin_store!r}: path characters, optionally under $HOME"
            )
        self._plugin_store = plugin_store.rstrip("/")
        self._closeout_tool_repo = closeout_tool_repo
        self._closeout_ledger_env = closeout_ledger_env
        self._closeout_registry_env = closeout_registry_env
        self._runtime_config = runtime_config_text
        self._runtime_config_target = runtime_config_target
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
        # Account steering (OMN-23636): asked once, at the draw, and the same arguments go to the engine's own
        # resolve. No steer wired, or a steer that answers None (the overlay switch is off), adds nothing.
        self._steer = login_steer
        self._resolve_arguments = ""
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
        self._guard_source = guard_host_source
        self._runner = runner or SubprocessHostCommandExecutor()
        self._ledger_names: dict[str, str] = {}

    def run_dir(self, run_id: str) -> str:
        return f"{self._root}/{run_id}"

    def _argv(self, host: ModelLaneHostRef, script: str) -> list[str]:
        if host.local:
            return ["sh", "-c", script]
        if not self._ssh_argv:
            raise ValueError("remote_lane_start.ssh_argv is required for a remote host")
        return [*self._ssh_argv, host.target or host.name, script]

    def _sh(
        self,
        host: ModelLaneHostRef,
        script: str,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        argv = self._argv(host, script)
        if stdin is None:
            return self._runner.run(argv, timeout or self._timeout)
        return self._runner.run(argv, timeout or self._timeout, stdin=stdin)

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
        steer = self._steer() if self._steer is not None else None
        self._resolve_arguments = (
            ""
            if steer is None
            else "".join(f" {shlex.quote(a)}" for a in steer.resolve_arguments())
        )
        script = (
            f"{self._path}{pool_python_sh(self._pool)}"
            f'if [ -z "$CPY" ]; then echo "POOL-RC=none"; exit 0; fi; '
            f'line=$("$CPY" -m {shlex.quote(self._pool.module)} resolve{self._resolve_arguments} 2>&1 >/dev/null); rc=$?; '
            f'echo "POOL-RC=$rc"; printf "%s\\n" "$line" | tail -n 1'
        )
        result = self._sh(host, script, self._pool.draw_timeout_seconds)
        lines = result.stdout.strip().splitlines()
        refused = _REFUSED_START.search(lines[-1]) if len(lines) >= 2 else None
        if (
            result.exit_code == 0
            and refused is not None
            and lines[-2] == f"POOL-RC={POOL_START_REFUSED_EXIT}"
        ):
            return ModelEngineLoginDraw(
                state="refused", refusal=refused.group(1), detail=lines[-1][:200]
            )
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
            + f'if [ -n "$CPY" ] && CP=$("$CPY" -m {q(self._pool.module)} resolve{self._resolve_arguments} 2>/dev/null); '
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

    def _run_dirs_header(self, run_id: str) -> str:
        """The runner's variable line: ``W`` the run directory and ``C`` its context directory."""
        run = self.run_dir(run_id)
        return f'W="{run}"; C="{run}.ctx"'

    def _failure(self, result: ModelHostCommandResult) -> str:
        return (result.stderr or result.stdout or f"exit {result.exit_code}").strip()[
            -300:
        ] or f"exit {result.exit_code}"

    def prepare_run_dirs(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneRunDirsResult:
        """The runner script's ``scratch_script``: the run directory and its ``.ctx`` beside it, made together."""
        run = self.run_dir(request.run_id)
        script = "\n".join(
            [
                self._run_dirs_header(request.run_id),
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

    def _context_dir(self, run_id: str) -> str:
        return f"{self.run_dir(run_id)}.ctx"

    def stage_ledger_snapshot(
        self, request: ModelLaneStageInputRequest
    ) -> ModelLaneStagedInput:
        """The runner's ``ledger_unpack_script``: unpack the snapshot into ``<run>.ctx/ledger``, read-only.

        ``request.name`` is the live ledger file's name and ``request.content`` the base64 of the gzip tar the
        runner's ``build_ledger_snapshot`` makes; the host receives the decoded bytes on stdin. On a host whose
        ledger of record is the bus (``ledger_write_via`` bus) the live ledger file stays owner-writable.
        """
        if not _STAGED_NAME.fullmatch(request.name):
            return ModelLaneStagedInput(
                staged=False,
                error=f"stage_ledger_snapshot: {request.name!r} is not a ledger file name",
            )
        try:
            archive = base64.b64decode(request.content, validate=True)
        except (binascii.Error, ValueError):
            return ModelLaneStagedInput(
                staged=False,
                error="stage_ledger_snapshot: content is not base64 of the snapshot archive",
            )
        ctx = self._context_dir(request.run_id)
        script = "\n".join(
            [
                f'C="{ctx}"',
                'mkdir -p "$C/ledger" || exit 70',
                'tar -xzf - -C "$C/ledger" || exit 70',
                'chmod a-w "$C/ledger/"*.md "$C/ledger/archive/"*.md 2>/dev/null',
                *(
                    [f'chmod u+w "$C/ledger/{request.name}" || exit 70']
                    if self._ledger_via == "bus"
                    else []
                ),
                'echo "LEDGER-SNAPSHOT $C/ledger"',
            ]
        )
        result = self._sh(request.host, script, request.timeout_seconds, stdin=archive)
        printed = next(
            (
                line.strip()[len(SNAPSHOT_PRINTED) :]
                for line in result.stdout.splitlines()
                if line.startswith(SNAPSHOT_PRINTED)
            ),
            None,
        )
        if result.exit_code == 0 and printed:
            self._ledger_names[request.run_id] = request.name
            return ModelLaneStagedInput(staged=True, path=printed)
        return ModelLaneStagedInput(staged=False, error=self._failure(result))

    def stage_file(self, request: ModelLaneStageInputRequest) -> ModelLaneStagedInput:
        """The runner's ``put_file``: write ``request.content`` to ``<run>.ctx/<name>`` through a temp file and a rename."""
        if not _STAGED_NAME.fullmatch(request.name):
            return ModelLaneStagedInput(
                staged=False,
                error=f"stage_file: {request.name!r} is not a plain file name",
            )
        target = f"{self._context_dir(request.run_id)}/{request.name}"
        script = "\n".join(
            [
                "set -eu",
                f'T=$(mktemp "{target}.XXXXXX")',
                "trap 'rm -f -- \"$T\"' EXIT",
                'cat > "$T"',
                *(
                    [f'chmod {request.mode:o} "$T"']
                    if request.mode != STAGED_DEFAULT_MODE
                    else []
                ),
                f'mv -f -- "$T" "{target}"',
            ]
        )
        result = self._sh(
            request.host,
            script,
            request.timeout_seconds,
            stdin=request.content.encode("utf-8"),
        )
        if result.exit_code == 0:
            return ModelLaneStagedInput(staged=True, path=target)
        return ModelLaneStagedInput(staged=False, error=self._failure(result))

    def ship_plugin_trees(
        self, request: ModelLaneShipPluginTreesRequest
    ) -> ModelLaneShipPluginTreesResult:
        """The runner's ``plugin_ensure_script``, filled from the caller's archives instead of a git fetch.

        Each tree is one call: its gzip tar archive (``archive_path`` on the caller) goes on stdin, is extracted into
        a temporary directory beside ``<plugin_store>/<name>-<digest>``, and renamed into place unless that tree is
        already there; the tree is touched (its last use) and ``PLUGIN-DIR <name> <dir>`` comes back. A tree that
        carries a lock file needs the runner's shared virtualenv, which this transport does not build: it is refused
        by name rather than shipped without one.
        """
        if not self._plugin_store:
            return ModelLaneShipPluginTreesResult(
                shipped=False, error=self._not_run("ship_plugin_trees")
            )
        dirs: list[tuple[str, str]] = []
        for tree in request.trees:
            if not _PLUGIN_NAME.fullmatch(tree.name):
                return ModelLaneShipPluginTreesResult(
                    shipped=False,
                    error=f"ship_plugin_trees: {tree.name!r} is not a plugin name",
                )
            if not _TREE_DIGEST.fullmatch(tree.source_digest):
                return ModelLaneShipPluginTreesResult(
                    shipped=False,
                    error=f"ship_plugin_trees: {tree.name}: the digest is not at least 12 letters or digits",
                )
            try:
                archive = Path(tree.archive_path).read_bytes()
            except OSError as exc:
                return ModelLaneShipPluginTreesResult(
                    shipped=False,
                    error=f"ship_plugin_trees: {tree.name}: archive unreadable: {exc}",
                )
            tree_dir = f"{self._plugin_store}/{tree.name}-{tree.source_digest[:12]}"
            script = "\n".join(
                [
                    f'{self._path}S="{self._plugin_store}"; mkdir -p "$S" || exit 70',
                    f'D="{tree_dir}"',
                    'if [ ! -d "$D" ]; then',
                    '  T="$D.tmp.$$"; rm -rf "$T"; mkdir -p "$T" || exit 70',
                    '  tar -xzf - -C "$T" || exit 70',
                    '  python3 -c "import os,sys; os.rename(sys.argv[1], sys.argv[2])" "$T" "$D" 2>/dev/null || rm -rf "$T"',
                    "else",
                    "  cat >/dev/null",
                    "fi",
                    'touch "$D" 2>/dev/null || true',
                    f'if [ -f "$D/{PLUGIN_LOCK_FILE}" ]; then echo "{PLUGIN_VENV_NOT_RUN}{tree.name}"; exit {PLUGIN_VENV_EXIT}; fi',
                    f'echo "{PLUGIN_DIR_PRINTED}{tree.name} $D"',
                ]
            )
            result = self._sh(
                request.host, script, request.timeout_seconds, stdin=archive
            )
            printed = next(
                (
                    line.split(" ", 2)
                    for line in result.stdout.splitlines()
                    if line.startswith(PLUGIN_DIR_PRINTED)
                ),
                None,
            )
            if result.exit_code != 0 or printed is None or len(printed) != 3:
                return ModelLaneShipPluginTreesResult(
                    shipped=False,
                    error=f"ship_plugin_trees: {tree.name}: {self._failure(result)}",
                )
            dirs.append((printed[1], printed[2].strip()))
        return ModelLaneShipPluginTreesResult(shipped=True, plugin_dirs=tuple(dirs))

    def mirror_ledger(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        """The runner's ``ledger_mirror_script``: append the lane's CLAIM row (staged on the host as
        ``claim-row.txt``) to the lane's own ledger copy and print ``LEDGER-MIRROR ok|failed``.

        The spi request carries no row, so the caller stages it first with ``stage_file`` under
        ``CLAIM_ROW_FILE``; the script reads it from there. The recorded text is the bus variant when
        ``ledger_write_via`` is bus, the relock variant otherwise, with the runner's ``$LR`` prefix
        dropped and ``$LR/runs/<run>`` replaced by the context directory.
        """
        ledger_name = self._ledger_names.get(request.run_id)
        if ledger_name is None:
            return ModelLaneHostStepResult(
                ok=False, error="mirror_ledger: no ledger name remembered for the run"
            )
        ctx = self._context_dir(request.run_id)
        relock = self._ledger_via != "bus"
        path = f'"$C/ledger/{ledger_name}"'
        script = "\n".join(
            [
                f'C="{ctx}"; F={path}',
                f'exec < "$C/{CLAIM_ROW_FILE}" || {{ echo "{MIRROR_PRINTED_FAILED}"; exit 70; }}',
                *(['chmod u+w "$F" 2>/dev/null'] if relock else []),
                'if [ -s "$F" ] && [ -n "$(tail -c 1 "$F")" ]; then echo >> "$F"; fi',
                f"if cat >> {path}; then R=ok; else R=failed; fi",
                *(['chmod a-w "$F" 2>/dev/null'] if relock else []),
                f'echo "{MIRROR_PRINTED}$R"',
            ]
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        printed = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith(MIRROR_PRINTED)
            ),
            None,
        )
        if result.exit_code == 0 and printed == MIRROR_PRINTED_OK:
            return ModelLaneHostStepResult(ok=True, cells=(("ledger_mirror", "ok"),))
        if printed == MIRROR_PRINTED_FAILED:
            return ModelLaneHostStepResult(ok=False, error=printed)
        return ModelLaneHostStepResult(ok=False, error=self._failure(result))

    def write_runtime_config(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        """The runner's ``runtime_config_script``: install the launching host's runtime config on a host that has none.

        The config text is the transport's ``runtime_config_text`` and its place under the registry root the
        ``runtime_config_target`` (the overlay's values; the text is screened for credential-like text); a host's own copy is never replaced. The printed ``RUNTIME-CONFIG installed|present <path>`` line comes
        back as the ``runtime_config`` cell.
        """
        if not self._runtime_config or not self._runtime_config_target:
            return ModelLaneHostStepResult(
                ok=False, error=self._not_run("write_runtime_config")
            )
        if request.registry_root is None:
            return ModelLaneHostStepResult(
                ok=False, error="write_runtime_config: registry root not resolved"
            )
        if not _LITERAL_PATH.fullmatch(request.registry_root):
            return ModelLaneHostStepResult(
                ok=False,
                error=f"write_runtime_config: registry root {request.registry_root!r}: only path characters",
            )
        target = f"{request.registry_root}/{self._runtime_config_target}"
        script = "\n".join(
            [
                f"T={shlex.quote(target)}",
                'if [ -f "$T" ]; then cat >/dev/null; echo "RUNTIME-CONFIG present $T"; exit 0; fi',
                'mkdir -p "$(dirname "$T")" || exit 70',
                'cat > "$T.tmp.$$" && mv "$T.tmp.$$" "$T" || exit 70',
                'echo "RUNTIME-CONFIG installed $T"',
            ]
        )
        result = self._sh(
            request.host,
            script,
            request.timeout_seconds,
            stdin=self._runtime_config.encode("utf-8"),
        )
        printed = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.startswith(RUNTIME_CONFIG_PRINTED)
            ),
            None,
        )
        if result.exit_code == 0 and printed:
            return ModelLaneHostStepResult(
                ok=True, cells=(("runtime_config", printed),)
            )
        return ModelLaneHostStepResult(ok=False, error=self._failure(result))

    def drain_guard_spool(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        """The runner's ``lane_guard.drain_script``: print and remove the host reaper's queued records.

        Each printed ``LANE-GUARD-SPOOL <json>`` line comes back as a ``spool`` cell, in the order printed;
        the caller turns them into FRICTION rows as the runner does.
        """
        if not self._guard_source:
            return ModelLaneHostStepResult(
                ok=False, error=GUARD_SOURCE_MISSING.format(op="drain_guard_spool")
            )
        result = self._sh(
            request.host,
            guard_script(self._guard_source, "spool-drain"),
            request.timeout_seconds,
        )
        if result.exit_code != 0:
            return ModelLaneHostStepResult(ok=False, error=self._failure(result))
        return ModelLaneHostStepResult(
            ok=True,
            cells=tuple(
                ("spool", line.strip()[len(GUARD_SPOOL_PREFIX) :])
                for line in (result.stdout + result.stderr).splitlines()
                if line.strip().startswith(GUARD_SPOOL_PREFIX)
            ),
        )

    def relay_reports(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        """The relay handler's ``remote_script``: tar the lane's report files and print the bytes.

        The script is the handler's ``remote_script`` for the registry root and lane, with `` | base64``
        appended to its last line so the tar bytes survive the text channel; the printed
        ``RELAY-TAR\n<base64>`` comes back as the ``relay_output_b64`` cell.
        """
        if request.registry_root is None:
            return ModelLaneHostStepResult(
                ok=False, error="relay_reports: registry root not resolved"
            )
        script = relay_remote_script(request.registry_root, request.lane)
        lines = script.splitlines()
        lines[-1] = lines[-1] + " | base64"
        result = self._sh(request.host, "\n".join(lines), request.timeout_seconds)
        if result.exit_code == 0:
            return ModelLaneHostStepResult(
                ok=True,
                cells=(("relay_output_b64", "".join(result.stdout.split())),),
            )
        return ModelLaneHostStepResult(ok=False, error=self._failure(result))

    def read_ledger_outbox(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneLedgerOutboxRows:
        """The runner script's ``ledger_outbox_script``: print the rows the lane queued (nothing when it queued none)."""
        run = self.run_dir(request.run_id)
        result = self._sh(
            request.host,
            f'cat "{run}.ctx/{OUTBOX_FILE}" 2>/dev/null; true',
            request.timeout_seconds,
        )
        if result.exit_code != 0:
            return ModelLaneLedgerOutboxRows(error=self._failure(result))
        return ModelLaneLedgerOutboxRows(
            rows=tuple(line for line in result.stdout.splitlines() if line.strip())
        )

    def read_lane_changes(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneChangeList:
        """The runner script's ``changes_script``: the files each of the lane's trees changed, sorted and unique."""
        if request.registry_root is None:
            return ModelLaneChangeList(
                error="read_lane_changes: registry root not resolved"
            )
        result = self._sh(
            request.host,
            changes_script(request.lane, request.registry_root),
            request.timeout_seconds,
        )
        if result.exit_code != 0:
            return ModelLaneChangeList(error=self._failure(result))
        found = (
            CHANGES_LINE_RE.match(line.strip()) for line in result.stdout.splitlines()
        )
        return ModelLaneChangeList(
            changes=tuple(sorted({m.group(2).strip() for m in found if m}))
        )

    def closeout_worktrees(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneCloseoutCells:
        """The runner's ``closeout_script``: the host's own ``onex-lane-closeout`` once per tree under the lane's directory.

        The script is the moved ``closeout_script`` (the relay handler module), its first line's home-relative
        run root swapped for the declared one. Each printed ``LANE-CLOSEOUT`` line's cell comes back as a cell;
        ``LANE-CLOSEOUT-UNAVAILABLE`` is the ``unavailable`` flag, and a tree the closeout refused is the error
        (the cells of the other trees stay).
        """
        if not (
            self._closeout_tool_repo
            and self._closeout_ledger_env
            and self._closeout_registry_env
        ):
            return ModelLaneCloseoutCells(error=self._not_run("closeout_worktrees"))
        if request.registry_root is None:
            return ModelLaneCloseoutCells(
                error="closeout_worktrees: registry root not resolved"
            )
        if not _LITERAL_PATH.fullmatch(request.registry_root):
            return ModelLaneCloseoutCells(
                error=f"closeout_worktrees: registry root {request.registry_root!r}: only path characters"
            )
        script = closeout_script(
            request.run_id,
            request.lane,
            request.registry_root,
            self._ledger_names.get(request.run_id),
            tool_repo=self._closeout_tool_repo,
            ledger_env=self._closeout_ledger_env,
            registry_env=self._closeout_registry_env,
        )
        first, newline, rest = script.partition("\n")
        prefix = CLOSEOUT_HEADER_PREFIX.format(run_id=request.run_id)
        if not first.startswith(prefix):
            return ModelLaneCloseoutCells(
                error="closeout_worktrees: the closeout script no longer opens with the run directory line"
            )
        ctx = self._context_dir(request.run_id)
        result = self._sh(
            request.host,
            f'C="{ctx}"; {first[len(prefix) :]}{newline}{rest}',
            request.timeout_seconds,
        )
        if result.exit_code != 0:
            return ModelLaneCloseoutCells(error=self._failure(result))
        if closeout_unavailable(result.stdout):
            return ModelLaneCloseoutCells(unavailable=True)
        trees = parse_closeout(result.stdout)
        refused = [t for t in trees if not t["cell"]]
        return ModelLaneCloseoutCells(
            cells=tuple(t["cell"] for t in trees if t["cell"]),
            error=(
                "closeout_worktrees: refused "
                + "; ".join(
                    f"{t['tree']}: {t['note'] or 'rc=' + str(t['rc'])}" for t in refused
                )
                if refused
                else None
            ),
        )

    def hold_run_dir_guard(
        self, request: ModelLaneHostStepRequest
    ) -> ModelLaneHostStepResult:
        """The runner script's ``hold_run_dir_guard`` over its cleanup header: safe only when it prints ``LANE-RETENTION-SAFE``.

        A lane tree whose git dir lives inside the run directory ends the script with the printed
        ``LANE-WORKDIR kept`` line, which is the refusal returned here.
        """
        if request.registry_root is None:
            return ModelLaneHostStepResult(
                ok=False, error="hold_run_dir_guard: registry root not resolved"
            )
        script = _hold_run_dir_guard(
            f"{self._run_dirs_header(request.run_id)}\necho {RETENTION_SAFE_LINE}",
            lane=request.lane,
            registry_root=request.registry_root,
        )
        result = self._sh(request.host, script, request.timeout_seconds)
        if result.exit_code == 0 and RETENTION_SAFE_LINE in result.stdout.splitlines():
            return ModelLaneHostStepResult(ok=True)
        kept = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith("LANE-WORKDIR ")
            ),
            None,
        )
        return ModelLaneHostStepResult(
            ok=False, error=kept[:300] if kept else self._failure(result)
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
        """The runner's ``lane_guard.reap_script`` for the lane's unit: stop it and kill what carries its tag.

        Succeeds when the host printed the ``LANE-GUARD-STOP <json>`` line, which is returned as the ``stop`` cell;
        the runner reads the same line for its receipt.
        """
        if not self._guard_source:
            return ModelLaneHostStepResult(
                ok=False, error=GUARD_SOURCE_MISSING.format(op="stop_lane_guard")
            )
        unit = guard_unit_name(request.lane, request.run_id)
        result = self._sh(
            request.host,
            guard_script(self._guard_source, f"reap --unit {shlex.quote(unit)}"),
            request.timeout_seconds,
        )
        stop = next(
            (
                line.strip()[len(GUARD_STOP_PREFIX) :]
                for line in (result.stdout + result.stderr).splitlines()
                if line.strip().startswith(GUARD_STOP_PREFIX)
            ),
            None,
        )
        if result.exit_code == 0 and stop is not None:
            return ModelLaneHostStepResult(ok=True, cells=(("stop", stop),))
        return ModelLaneHostStepResult(ok=False, error=self._failure(result))


__all__ = [
    "SIGNALLED_EXIT",
    "STEP_NOT_RUN_BY_NODE",
    "ModelHostCommandResult",
    "ProtocolHostCommandRunner",
    "SshLaneHostTransport",
    "SubprocessHostCommandExecutor",
    "pool_python_sh",
]
