# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one orchestrator round as a headless ``claude -p``, once at a time.

The orchestrator's fill, integration and review rounds ran only as cron prompts inside one Claude
Code session, so when that session stalled no round fired and nothing alarmed. Until each round is
a scheduled node run, a launchd agent per round (rendered by node_host_installer_registry_units)
runs this node.

One start of a round:

1. Takes the round's lock: ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``<state>/<round>.lock``. The
   kernel holds the lock for the life of the open file and drops it when the holder exits or is
   killed, so a crashed run leaves no stale lock. When another live run holds it, the start records
   the refusal in ``<round>.skip.json`` and returns ``busy`` (exit ``EXIT_BUSY``) without running
   the round.
2. Writes the heartbeat ``<round>.heartbeat.json`` (``state: running`` with the pid and start
   time), runs ``claude -p <prompt> <claude arguments>`` in its own process group, ends that group
   when the timeout passes, and writes the heartbeat again (``finished`` or ``timed-out`` with the
   exit code). The round's output is kept in ``<round>.last.out``.

The heartbeat moves at every start and every end of a round and never on a refused start, so its
modification time is the "a round ran" fact a stalled-session alarm reads.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..models.model_session_round import (
    ModelSessionRoundRequest,
    ModelSessionRoundResult,
)

# EX_TEMPFAIL: the previous run of this round is alive; the next interval tries again.
EXIT_BUSY = 75
# The process group gets SIGKILL this long after SIGTERM.
KILL_GRACE_SECONDS = 30


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, record: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _holder(lock_fd: int) -> str:
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        return os.read(lock_fd, 64).decode("utf-8", "replace").strip()
    except OSError:
        return ""


class HandlerSessionRound:
    """Effect: one start of one round, guarded by that round's lock."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelSessionRoundRequest) -> ModelSessionRoundResult:
        state = request.state_dir
        state.mkdir(parents=True, exist_ok=True)
        name = request.round_name
        heartbeat = state / f"{name}.heartbeat.json"
        lock_fd = os.open(state / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = _holder(lock_fd)
                _write_json(
                    state / f"{name}.skip.json",
                    {
                        "round": name,
                        "refused_at": _stamp(self._clock()),
                        "holder_pid": holder,
                    },
                )
                return ModelSessionRoundResult(
                    round_name=name,
                    outcome="busy",
                    exit_code=EXIT_BUSY,
                    holder_pid=holder,
                    heartbeat_file=heartbeat,
                    detail=(
                        f"the previous run (pid {holder or 'unknown'}) holds the lock; "
                        "this start runs nothing"
                    ),
                )
            os.ftruncate(lock_fd, 0)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            os.write(lock_fd, f"{os.getpid()}\n".encode())
            started = _stamp(self._clock())
            _write_json(
                heartbeat,
                {
                    "round": name,
                    "state": "running",
                    "pid": os.getpid(),
                    "started_at": started,
                },
            )
            code, detail, outcome = self._run(request, state / f"{name}.last.out")
            record: dict[str, object] = {
                "round": name,
                "state": outcome,
                "pid": os.getpid(),
                "started_at": started,
                "finished_at": _stamp(self._clock()),
                "exit_code": code,
            }
            if detail:
                record["detail"] = detail
            _write_json(heartbeat, record)
            return ModelSessionRoundResult(
                round_name=name,
                outcome=outcome,
                exit_code=code,
                heartbeat_file=heartbeat,
                detail=detail,
            )
        finally:
            os.close(lock_fd)

    @staticmethod
    def _run(
        request: ModelSessionRoundRequest, output_file: Path
    ) -> tuple[int, str, str]:
        command = [request.claude, "-p", request.prompt, *request.claude_arguments]
        with output_file.open("w", encoding="utf-8") as output:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                output.write(f"cannot start {request.claude}: {exc}\n")
                return 127, f"cannot start {request.claude}: {exc}", "not-started"
            try:
                return process.wait(timeout=request.timeout_seconds), "", "finished"
            except subprocess.TimeoutExpired:
                for sig, wait in (
                    (signal.SIGTERM, KILL_GRACE_SECONDS),
                    (signal.SIGKILL, None),
                ):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                code = process.wait()
                return (
                    code,
                    f"timed out after {request.timeout_seconds} s",
                    "timed-out",
                )


__all__ = ["EXIT_BUSY", "HandlerSessionRound"]
