# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the remote-lane stop ports.

The transport is the runner's ssh (an argv prefix the request carries) plus signals to a pid on
this host. The ledger adapter calls the ledger writer the caller names and never appends a row
itself. Nothing here runs git or gh.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    ModelStopHost,
)

from .protocol_remote_lane_stop import RemoteLaneStopPortError, StopLedgerWrite

Run = Callable[..., Any]
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}
CLOUD_ENDED = frozenset({"idle", "requires_action", "archived"})
SSH_TIMEOUT_S = 60.0


def _ctx(host: ModelStopHost, run_id: str) -> str:
    return shlex.quote(f"{host.work_root}/runs/{run_id}.ctx")


def signal_script(host: ModelStopHost, run_id: str, sig: str) -> str:
    """Remote shell: signal the detached run's process group, unless it already ended.

    The run's exit-code file is the proof it ended; a pid file left behind may name a recycled
    pid, which this never signals.
    """
    verb = "kill -KILL" if sig == "KILL" else "kill -TERM"
    return "\n".join(
        [
            f"C={_ctx(host, run_id)}",
            "# STOP-SIGNAL",
            'if [ -s "$C/run.rc" ] || [ ! -s "$C/run.pid" ]; then echo STOP-NONE; exit 0; fi',
            'P=$(cat "$C/run.pid")',
            f'if kill -0 "$P" 2>/dev/null; then {verb} -- "-$P" 2>/dev/null; echo STOP-SIGNALLED;',
            "else echo STOP-NONE; fi",
        ]
    )


def alive_script(host: ModelStopHost, run_id: str) -> str:
    """Remote shell: print ``TREE-ALIVE <pgid>`` while any process of the run's group lives."""
    return "\n".join(
        [
            f"C={_ctx(host, run_id)}",
            "# TREE-ALIVE",
            'if [ -s "$C/run.pid" ]; then P=$(cat "$C/run.pid"); else echo TREE-GONE; exit 0; fi',
            'if kill -0 -- "-$P" 2>/dev/null || kill -0 "$P" 2>/dev/null; then echo "TREE-ALIVE $P";',
            "else echo TREE-GONE; fi",
        ]
    )


class LocalStopTransport:
    """Signals over the runner's ssh transport or to a pid on this host."""

    def __init__(self, run: Run = subprocess.run) -> None:
        self._run = run

    def _ssh(self, host: ModelStopHost, script: str) -> str:
        if not host.ssh_argv:
            raise RemoteLaneStopPortError(
                f"the request gives no ssh transport for host {host.name}"
            )
        try:
            done = self._run(
                [*host.ssh_argv, "bash", "-s"],
                input=script,
                capture_output=True,
                text=True,
                check=False,
                timeout=SSH_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteLaneStopPortError(
                f"ssh to {host.name}: {type(exc).__name__}: {exc}"
            ) from exc
        if done.returncode != 0:
            raise RemoteLaneStopPortError(
                f"ssh to {host.name} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}"
            )
        return str(done.stdout)

    def signal_lab(self, host: ModelStopHost, run_id: str, sig: str) -> bool:
        return "STOP-SIGNALLED" in self._ssh(host, signal_script(host, run_id, sig))

    def lab_tree_alive(self, host: ModelStopHost, run_id: str) -> str | None:
        found = re.search(
            r"TREE-ALIVE (\S+)", self._ssh(host, alive_script(host, run_id))
        )
        return f"process group {found.group(1)} on {host.name}" if found else None

    def reap_lab_unit(self, host: ModelStopHost, unit: str) -> None:
        if host.reap_script:
            self._ssh(host, host.reap_script.replace("{unit}", shlex.quote(unit)))

    def signal_local(self, pid: int, sig: str) -> None:
        number = SIGNALS[sig]
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():  # never this process's own group
                os.killpg(pgid, number)
            else:
                os.kill(pid, number)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise RemoteLaneStopPortError(f"signal {sig} to pid {pid}: {exc}") from exc

    def local_alive(self, pid: int) -> bool:
        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):  # a child of ours that has ended
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        state = self._run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return not str(state.stdout).strip().startswith("Z")

    def remove_slot_markers(self, placement_dir: str, host: str, pid: int) -> list[str]:
        """Remove the lane's placement markers, ``<host>.<id>.<pid>``, so placement counts the slot free."""
        removed: list[str] = []
        directory = Path(placement_dir)
        if not directory.is_dir():
            return removed
        for marker in sorted(directory.iterdir()):
            if marker.name.rsplit(".", 2) == [
                host,
                marker.name.split(".")[1],
                str(pid),
            ]:
                marker.unlink(missing_ok=True)
                removed.append(marker.name)
        return removed


class LocalStopLedger:
    """Reads the ledger file and writes a TERMINAL by calling the ledger writer the caller names."""

    def __init__(self, run: Run = subprocess.run) -> None:
        self._run = run

    def read_text(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RemoteLaneStopPortError(
                f"cannot read the ledger {path}: {exc}"
            ) from exc

    def write_terminal(
        self, command: list[str], path: str, cells: list[tuple[str, str]], text: str
    ) -> StopLedgerWrite:
        argv = [*command, "row", "TERMINAL"]
        for flag, value in cells:
            argv += [flag, value]
        argv += ["--ledger", path, "--text", text]
        try:
            done = self._run(
                argv, capture_output=True, text=True, check=False, timeout=360
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteLaneStopPortError(
                f"the ledger writer: {type(exc).__name__}: {exc}"
            ) from exc
        output = (str(done.stdout) + str(done.stderr)).strip()
        stamp = TS_RE.search(str(done.stdout)) if done.returncode == 0 else None
        return StopLedgerWrite(
            accepted=done.returncode == 0 and stamp is not None,
            ts=stamp.group(0) if stamp else None,
            output=output[-300:],
        )


class CommandStopCloud:
    """Ends a cloud session through a command the caller names: ``archive`` and ``status``."""

    def __init__(self, command: list[str], run: Run = subprocess.run) -> None:
        self._command = command
        self._run = run

    def _call(self, verb: str, session_id: str, account: str) -> str:
        argv = [*self._command, verb, "--session", session_id, "--account", account]
        try:
            done = self._run(
                argv, capture_output=True, text=True, check=False, timeout=120
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteLaneStopPortError(
                f"cloud {verb}: {type(exc).__name__}: {exc}"
            ) from exc
        if done.returncode != 0:
            raise RemoteLaneStopPortError(
                f"cloud {verb} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}"
            )
        return str(done.stdout)

    def archive(self, session_id: str, account: str) -> None:
        self._call("archive", session_id, account)

    def session_ended(self, session_id: str, account: str) -> bool:
        return self._call("status", session_id, account).strip().lower() in CLOUD_ENDED
