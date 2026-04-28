# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live workflow runner for node_pr_polish.

The pure FSM in ``handler_pr_polish.py`` remains useful for unit coverage, but
the real branch-polishing path needs explicit repo/worktree execution. This
module owns that live path:

- resolve the correct worktree for ``repo`` + ``pr_number``
- verify checkout matches the PR head branch
- install pre-commit hooks in the worktree
- run the authoritative ``/onex:pr_polish`` skill inside that worktree
- persist ``result.json`` under the dispatched run directory
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omnimarket.nodes.node_pr_polish.models.model_pr_polish_completed_event import (
    ModelPrPolishCompletedEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def run_live_pr_polish(
    command: ModelPrPolishStartCommand,
    *,
    claude_bin: str | None = None,
) -> ModelPrPolishCompletedEvent:
    """Run the live repo/worktree-aware polish workflow and persist result.json."""
    started_at = datetime.now(tz=UTC)
    run_dir = _resolve_run_dir(command)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "worker.log"

    payload: dict[str, object] = {
        "correlation_id": str(command.correlation_id),
        "repo": command.repo,
        "pr_number": command.pr_number,
        "ticket_id": command.ticket_id,
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "requested_at": command.requested_at.isoformat(),
        "started_at": started_at.isoformat(),
        "dry_run": command.dry_run,
    }

    try:
        if not command.repo:
            raise RuntimeError("repo is required for live pr_polish execution")
        if command.pr_number is None:
            raise RuntimeError("pr_number is required for live pr_polish execution")

        worktree = _resolve_worktree_path(command)
        expected_branch = _resolve_pr_head_branch(command.repo, command.pr_number)
        branch_before = _git_stdout(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=15,
        )
        if branch_before != expected_branch:
            _run_checked(
                ["git", "-C", str(worktree), "checkout", expected_branch],
                timeout=30,
            )
        branch_after = _git_stdout(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=15,
        )
        if branch_after != expected_branch:
            raise RuntimeError(
                f"worktree branch mismatch: expected {expected_branch!r}, got {branch_after!r}"
            )

        _run_checked(["pre-commit", "install"], cwd=worktree, timeout=60)
        skill_cmd = _build_skill_command(command)
        _run_skill(
            claude_bin=claude_bin or os.environ.get("CLAUDE_BIN", "claude"),
            skill_cmd=skill_cmd,
            worktree=worktree,
            log_path=log_path,
        )

        completed = ModelPrPolishCompletedEvent(
            correlation_id=command.correlation_id,
            final_phase=EnumPrPolishPhase.DONE,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pr_number=command.pr_number,
        )
        payload.update(
            {
                "final_state": "COMPLETE",
                "worktree_path": str(worktree),
                "expected_branch": expected_branch,
                "actual_branch": branch_after,
                "skill_command": skill_cmd,
                "completed_at": completed.completed_at.isoformat(),
            }
        )
    except Exception as exc:
        completed = ModelPrPolishCompletedEvent(
            correlation_id=command.correlation_id,
            final_phase=EnumPrPolishPhase.FAILED,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pr_number=command.pr_number,
            error_message=str(exc),
        )
        payload.update(
            {
                "final_state": "FAILED",
                "error_message": str(exc),
                "completed_at": completed.completed_at.isoformat(),
            }
        )

    payload["completed_event"] = completed.model_dump(mode="json")
    (run_dir / "result.json").write_text(json.dumps(payload, indent=2))
    return completed


def _resolve_run_dir(command: ModelPrPolishStartCommand) -> Path:
    if command.run_dir:
        return Path(command.run_dir)
    state_dir = Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))
    repo_slug = (command.repo or "unknown-repo").replace("/", "-")
    pr_part = str(command.pr_number) if command.pr_number is not None else "unknown-pr"
    run_id = uuid4().hex[:12]
    return state_dir / "pr-polish" / f"{repo_slug}-{pr_part}-{run_id}"


def _resolve_worktree_path(command: ModelPrPolishStartCommand) -> Path:
    if command.worktree_path:
        path = Path(command.worktree_path)
        if not path.exists():
            raise RuntimeError(f"worktree_path does not exist: {path}")
        return path
    assert command.repo is not None
    assert command.pr_number is not None
    expected_branch = _resolve_pr_head_branch(command.repo, command.pr_number)
    output = _run_checked(
        ["git", "worktree", "list", "--porcelain"],
        cwd=_REPO_ROOT,
        timeout=15,
    )
    for block in output.split("\n\n"):
        raw_path = ""
        branch = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                raw_path = line[9:].strip()
            elif line.startswith("branch "):
                branch = line[7:].strip()
        if branch.endswith(f"/{expected_branch}") or branch == expected_branch:
            candidate = Path(raw_path)
            if candidate.exists():
                return candidate
    raise RuntimeError(
        f"no git worktree found for {command.repo}#{command.pr_number} branch {expected_branch!r}"
    )


def _resolve_pr_head_branch(repo: str, pr_number: int) -> str:
    output = _run_checked(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName",
        ],
        cwd=_REPO_ROOT,
        timeout=30,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr view returned invalid JSON for {repo}#{pr_number}"
        ) from exc
    branch = str(payload.get("headRefName") or "").strip()
    if not branch:
        raise RuntimeError(
            f"gh pr view returned empty headRefName for {repo}#{pr_number}"
        )
    return branch


def _build_skill_command(command: ModelPrPolishStartCommand) -> str:
    if command.pr_number is None:
        raise RuntimeError("pr_number is required to build pr_polish skill command")
    parts = ["/onex:pr_polish", str(command.pr_number)]
    if command.required_clean_runs != 4:
        parts.extend(["--required-clean-runs", str(command.required_clean_runs)])
    if command.max_iterations != 10:
        parts.extend(["--max-iterations", str(command.max_iterations)])
    if command.skip_conflicts:
        parts.append("--skip-conflicts")
    if command.skip_pr_review:
        parts.append("--skip-pr-review")
    if command.skip_local_review:
        parts.append("--skip-local-review")
    if command.no_ci:
        parts.append("--no-ci")
    if command.no_push:
        parts.append("--no-push")
    if command.no_automerge:
        parts.append("--no-automerge")
    if command.dry_run:
        parts.append("--dry-run")
    return " ".join(parts)


def _run_skill(
    *,
    claude_bin: str,
    skill_cmd: str,
    worktree: Path,
    log_path: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with log_path.open("ab") as log_fh:
        proc = subprocess.run(
            [claude_bin, "-p", skill_cmd],
            cwd=worktree,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=3600,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pr_polish skill failed with exit {proc.returncode}; see {log_path}"
        )


def _git_stdout(argv: list[str], *, timeout: int) -> str:
    return _run_checked(argv, timeout=timeout).strip()


def _run_checked(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
) -> str:
    env = os.environ.copy()
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        first_line = detail[0] if detail else ""
        raise RuntimeError(
            f"{' '.join(argv)} failed with exit {proc.returncode}: {first_line}"
        )
    return proc.stdout


__all__ = ["run_live_pr_polish"]
