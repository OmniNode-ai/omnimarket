# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live node-dispatch adapter for pr_lifecycle_fix_effect.

Spawns detached workers for PR review-fix and CodeRabbit auto-reply flows.
Review-fix now dispatches the real ``node_pr_polish`` CLI so the node owns
repo/worktree resolution and result persistence. Related: OMN-9284, OMN-10180.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
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
        cwd: str | None,
    ) -> object: ...


def _default_spawner(
    argv: list[str],
    *,
    stdout: int,
    stderr: int,
    start_new_session: bool,
    env: dict[str, str] | None,
    cwd: str | None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
        env=env,
        cwd=cwd,
    )


class PrPolishDispatchAdapter:
    """Dispatch market-owned PR polish and CodeRabbit triage node workers.

    Each dispatch:
      * Creates ``$ONEX_STATE_DIR/pr-polish/{repo_slug}-{pr}-{run_id}/`` and
        writes a ``dispatch.json`` breadcrumb so subsequent ticks can see that
        a worker was actually spawned (the signal that was missing pre-9284).
      * Spawns an ``omnimarket.nodes.*`` module detached (``start_new_session=True``,
        stdout/stderr redirected to a log file inside the state dir). The
        orchestrator does not block on the worker.
    """

    def __init__(
        self,
        *,
        python_bin: str | None = None,
        state_dir: Path | None = None,
        spawner: ProtocolSubprocessSpawner | None = None,
    ) -> None:
        self._python_bin = python_bin or sys.executable
        self._state_dir = state_dir or self._resolve_state_dir()
        self._repo_root = Path(__file__).resolve().parents[5]
        self._src_root = self._repo_root / "src"
        self._spawner: ProtocolSubprocessSpawner = spawner or _default_spawner

    @staticmethod
    def _resolve_state_dir() -> Path:
        return Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        return self._spawn_review_fix(repo, pr_number, ticket_id)

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        return self._spawn_coderabbit_reply(repo, pr_number)

    def _spawn_review_fix(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self._make_run_dir(repo, pr_number, run_id)
        log_path = run_dir / "worker.log"
        argv = [
            self._python_bin,
            "-m",
            "omnimarket.nodes.node_pr_polish",
            "--repo",
            repo,
            "--pr-number",
            str(pr_number),
            "--run-dir",
            str(run_dir),
        ]
        if ticket_id:
            argv.extend(["--ticket", ticket_id])
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{self._src_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(self._src_root)
        )
        self._spawn_process(
            kind="review-fix",
            repo=repo,
            pr_number=pr_number,
            ticket_id=ticket_id,
            run_id=run_id,
            run_dir=run_dir,
            log_path=log_path,
            argv=argv,
            env=env,
            cwd=str(self._repo_root),
        )
        return f"dispatched review-fix agent on {repo}#{pr_number} run_id={run_id}"

    def _spawn_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self._make_run_dir(repo, pr_number, run_id)
        log_path = run_dir / "worker.log"
        argv = [
            self._python_bin,
            "-m",
            "omnimarket.nodes.node_coderabbit_triage",
            "--repo",
            repo,
            "--pr",
            str(pr_number),
        ]
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{self._src_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(self._src_root)
        )
        self._spawn_process(
            kind="coderabbit-reply",
            repo=repo,
            pr_number=pr_number,
            ticket_id=None,
            run_id=run_id,
            run_dir=run_dir,
            log_path=log_path,
            argv=argv,
            env=env,
            cwd=str(self._repo_root),
        )
        return (
            f"dispatched coderabbit-reply agent on {repo}#{pr_number} run_id={run_id}"
        )

    def _spawn_process(
        self,
        *,
        kind: str,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        run_id: str,
        run_dir: Path,
        log_path: Path,
        argv: list[str],
        env: dict[str, str] | None,
        cwd: str | None,
    ) -> None:
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
                    env=env,
                    cwd=cwd,
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
