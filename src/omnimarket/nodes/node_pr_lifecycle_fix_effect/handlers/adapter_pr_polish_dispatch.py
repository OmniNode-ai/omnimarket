# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live agent-dispatch adapter for pr_lifecycle_fix_effect.

Spawns detached ``claude -p`` sub-agents for PR review-fix and CodeRabbit
auto-reply flows. Before this adapter existed, the orchestrator wired
``_NoopAgentDispatchAdapter`` which returned descriptive strings without
spawning any subprocess — BLOCKED PRs silently accumulated. Related:
OMN-9284, mirrors the OMN-9276 pattern.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _terminate_spawned(proc_handle: object) -> None:
    """Best-effort terminate + kill of a spawned subprocess handle.

    Called from the breadcrumb-write-failure path to prevent a live worker
    from running without a dispatch breadcrumb on disk. Swallows every
    exception: this runs on the error path and must never mask the original
    failure. ``proc_handle`` is typed ``object`` because the spawner seam
    (``ProtocolSubprocessSpawner``) returns ``object``; we duck-type the
    Popen-like methods.
    """
    terminate = getattr(proc_handle, "terminate", None)
    if callable(terminate):
        with contextlib.suppress(Exception):
            terminate()
    wait = getattr(proc_handle, "wait", None)
    if callable(wait):
        with contextlib.suppress(Exception):
            wait(timeout=1.0)
    poll = getattr(proc_handle, "poll", None)
    still_running = True
    if callable(poll):
        with contextlib.suppress(Exception):
            still_running = poll() is None
    if still_running:
        kill = getattr(proc_handle, "kill", None)
        if callable(kill):
            with contextlib.suppress(Exception):
                kill()


@runtime_checkable
class ProtocolSubprocessSpawner(Protocol):
    """Seam that lets tests assert on Popen args without actually spawning."""

    def __call__(
        self,
        argv: list[str],
        *,
        stdout: int,
        stderr: int,
        start_new_session: bool,
        env: dict[str, str] | None,
    ) -> object: ...


def _default_spawner(
    argv: list[str],
    *,
    stdout: int,
    stderr: int,
    start_new_session: bool,
    env: dict[str, str] | None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
        env=env,
    )


class PrPolishDispatchAdapter:
    """Dispatch ``/onex:pr_polish`` and ``/onex:coderabbit_triage`` sub-agents.

    Each dispatch:
      * Creates ``$ONEX_STATE_DIR/pr-polish/{repo_slug}-{pr}-{run_id}/`` and
        writes a ``dispatch.json`` breadcrumb so subsequent ticks can see that
        a worker was actually spawned (the signal that was missing pre-9284).
      * Spawns ``claude -p '<skill invocation>'`` detached
        (``start_new_session=True``, stdout/stderr redirected to a log file
        inside the state dir). The orchestrator does not block on the worker.
    """

    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        state_dir: Path | None = None,
        spawner: ProtocolSubprocessSpawner | None = None,
    ) -> None:
        self._claude_bin = claude_bin or os.environ.get("CLAUDE_BIN", "claude")
        self._state_dir = state_dir or self._resolve_state_dir()
        self._spawner: ProtocolSubprocessSpawner = spawner or _default_spawner

    @staticmethod
    def _resolve_state_dir() -> Path:
        return Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        skill_cmd = f"/onex:pr_polish --repo {repo} --pr {pr_number}"
        if ticket_id:
            skill_cmd += f" --ticket {ticket_id}"
        return self._spawn("review-fix", repo, pr_number, skill_cmd, ticket_id)

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        skill_cmd = f"/onex:coderabbit_triage --repo {repo} --pr {pr_number}"
        return self._spawn("coderabbit-reply", repo, pr_number, skill_cmd, None)

    def _spawn(
        self,
        kind: str,
        repo: str,
        pr_number: int,
        skill_cmd: str,
        ticket_id: str | None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self._make_run_dir(repo, pr_number, run_id)
        log_path = run_dir / "worker.log"
        argv = [self._claude_bin, "-p", skill_cmd]

        # Spawn first; only write the breadcrumb after the child has actually
        # started. Writing dispatch.json before the spawn would recreate the
        # exact false-positive OMN-9284 set out to eliminate — a later tick
        # would see dispatch.json and assume a worker ran when none did.
        try:
            log_fh = log_path.open("ab")
        except OSError as exc:
            raise RuntimeError(
                f"failed to dispatch {kind} agent on {repo}#{pr_number}: "
                f"could not open log file {log_path}: {exc}"
            ) from exc
        proc_handle: object
        try:
            try:
                proc_handle = self._spawner(
                    argv,
                    stdout=log_fh.fileno(),
                    stderr=log_fh.fileno(),
                    start_new_session=True,
                    env=None,
                )
            except OSError as exc:
                raise RuntimeError(
                    f"failed to dispatch {kind} agent on {repo}#{pr_number}: {exc}"
                ) from exc
        finally:
            log_fh.close()

        # Transactionally persist the breadcrumb — if it fails, kill the
        # spawned worker so handler_pr_lifecycle_fix sees an all-or-nothing
        # transition rather than a silent live-worker-without-breadcrumb
        # leak (which would let a later tick spawn a duplicate).
        try:
            self._write_breadcrumb(
                run_dir, kind, repo, pr_number, ticket_id, argv, run_id
            )
        except OSError as exc:
            _terminate_spawned(proc_handle)
            raise RuntimeError(
                f"failed to dispatch {kind} agent on {repo}#{pr_number}: "
                f"breadcrumb write failed, spawned worker killed: {exc}"
            ) from exc

        logger.info(
            "pr_polish_dispatch: kind=%s repo=%s pr=%s run_id=%s state_dir=%s",
            kind,
            repo,
            pr_number,
            run_id,
            run_dir,
        )
        return f"dispatched {kind} agent on {repo}#{pr_number} run_id={run_id}"

    def _make_run_dir(self, repo: str, pr_number: int, run_id: str) -> Path:
        slug = repo.replace("/", "-")
        run_dir = self._state_dir / "pr-polish" / f"{slug}-{pr_number}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _write_breadcrumb(
        run_dir: Path,
        kind: str,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        argv: list[str],
        run_id: str,
    ) -> None:
        import json

        payload = {
            "kind": kind,
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": ticket_id,
            "argv": argv,
            "run_id": run_id,
            "dispatched_at": datetime.now(tz=UTC).isoformat(),
        }
        (run_dir / "dispatch.json").write_text(json.dumps(payload, indent=2))


__all__ = ["PrPolishDispatchAdapter", "ProtocolSubprocessSpawner"]
