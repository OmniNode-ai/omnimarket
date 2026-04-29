# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live workflow runner for node_pr_polish.

The pure FSM in ``handler_pr_polish.py`` remains useful for unit coverage, but
the real branch-polishing path needs explicit repo/worktree execution. This
module owns that live path:

- resolve the correct worktree for ``repo`` + ``pr_number``
- verify checkout matches the PR head branch
- install pre-commit hooks in the worktree
- run market-owned deterministic polish phases inside that worktree
- persist ``result.json`` under the dispatched run directory
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
    ModelCoderabbitTriageResult,
)
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
        if command.required_clean_runs > command.max_iterations:
            raise RuntimeError("required_clean_runs cannot exceed max_iterations")

        worktree = _resolve_worktree_path(command)
        expected_branch = _resolve_pr_head_branch(command.repo, command.pr_number)
        branch_before = _git_stdout(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=15,
        )
        if branch_before != expected_branch:
            if command.dry_run:
                raise RuntimeError(
                    "dry_run requires the worktree to already be on the PR head branch"
                )
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

        if not command.dry_run:
            _run_checked(["pre-commit", "install"], cwd=worktree, timeout=60)
        phase_results = _run_market_polish_phases(
            command,
            worktree=worktree,
            log_path=log_path,
        )
        payload["phase_results"] = phase_results

        coderabbit_result = _run_coderabbit_triage(
            command.repo,
            command.pr_number,
            correlation_id=str(command.correlation_id),
            dry_run=command.dry_run or command.no_push,
        )
        payload["coderabbit_triage"] = coderabbit_result.model_dump(mode="json")

        if command.no_push or command.dry_run:
            payload["push_status"] = "skipped"
            payload["auto_merge_status"] = "skipped"
        else:
            _run_checked(
                ["uv", "run", "pre-commit", "run", "--all-files"],
                cwd=worktree,
                timeout=1800,
            )
            payload["pre_push_pre_commit"] = "passed"
            push_branch = _resolve_pr_head_branch(command.repo, command.pr_number)
            _run_checked(
                ["git", "-C", str(worktree), "push", "origin", f"HEAD:{push_branch}"],
                timeout=300,
            )
            payload["push_status"] = "pushed"
            payload["push_branch"] = push_branch
            local_sha = _git_stdout(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                timeout=15,
            )
            remote_sha = _resolve_pr_head_sha(command.repo, command.pr_number)
            payload["local_head_sha"] = local_sha
            payload["remote_head_sha"] = remote_sha
            if local_sha != remote_sha:
                raise RuntimeError(
                    "post-push SHA mismatch: "
                    f"local HEAD {local_sha[:8]} != remote PR head {remote_sha[:8]}"
                )
            if coderabbit_result.has_blockers:
                payload["auto_merge_status"] = "blocked_by_coderabbit"
            elif command.no_automerge:
                payload["auto_merge_status"] = "skipped"
            else:
                _enable_auto_merge(command.repo, command.pr_number)
                payload["auto_merge_status"] = "armed"

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
    if command.repo is None:
        raise RuntimeError("repo is required to resolve worktree path")
    if command.pr_number is None:
        raise RuntimeError("pr_number is required to resolve worktree path")
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


def _run_market_polish_phases(
    command: ModelPrPolishStartCommand,
    *,
    worktree: Path,
    log_path: Path,
) -> list[dict[str, object]]:
    """Run deterministic market-owned polish phases.

    This intentionally does not shell to Claude, Codex, or slash-command
    adapters. A future code-modifying repair step must be another OmniNode node
    with its own contract and evidence surface.
    """
    results: list[dict[str, object]] = []
    if command.skip_conflicts:
        results.append({"phase": "resolve_conflicts", "status": "skipped"})
    else:
        unmerged = _git_stdout(
            ["git", "-C", str(worktree), "diff", "--name-only", "--diff-filter=U"],
            timeout=30,
        )
        if unmerged:
            raise RuntimeError(
                "unmerged conflict paths remain; deterministic pr_polish will not "
                f"guess a resolution: {unmerged}"
            )
        results.append({"phase": "resolve_conflicts", "status": "clean"})

    if command.no_ci:
        results.append({"phase": "fix_ci", "status": "skipped"})
    else:
        results.append(
            {
                "phase": "fix_ci",
                "status": "deferred_to_required_checks",
                "detail": "CI state is enforced by GitHub required checks before merge.",
            }
        )

    if command.skip_pr_review:
        results.append({"phase": "address_comments", "status": "skipped"})
    else:
        results.append(
            {
                "phase": "address_comments",
                "status": "handled_by_market_coderabbit_triage",
            }
        )

    if command.skip_local_review:
        results.append({"phase": "local_review", "status": "skipped"})
    elif command.dry_run or command.no_push:
        results.append(
            {
                "phase": "local_review",
                "status": "skipped_non_mutating_mode",
                "detail": "Use push mode to run local review before publishing.",
            }
        )
    else:
        _run_checked(
            ["uv", "run", "pre-commit", "run", "--all-files"],
            cwd=worktree,
            timeout=1800,
        )
        _append_log_line(log_path, "market local_review: pre-commit passed")
        results.append({"phase": "local_review", "status": "passed"})

    return results


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


def _resolve_pr_head_sha(repo: str, pr_number: int) -> str:
    output = _run_checked(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefOid",
        ],
        cwd=_REPO_ROOT,
        timeout=30,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr view returned invalid JSON headRefOid for {repo}#{pr_number}"
        ) from exc
    sha = str(payload.get("headRefOid") or "").strip()
    if not sha:
        raise RuntimeError(
            f"gh pr view returned empty headRefOid for {repo}#{pr_number}"
        )
    return sha


def _resolve_pr_node_id(repo: str, pr_number: int) -> str:
    output = _run_checked(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "id",
        ],
        cwd=_REPO_ROOT,
        timeout=30,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr view returned invalid JSON id for {repo}#{pr_number}"
        ) from exc
    node_id = str(payload.get("id") or "").strip()
    if not node_id:
        raise RuntimeError(f"gh pr view returned empty id for {repo}#{pr_number}")
    return node_id


def _enable_auto_merge(repo: str, pr_number: int) -> None:
    pr_node_id = _resolve_pr_node_id(repo, pr_number)
    graphql = (
        "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
        "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
        "    pullRequest { number }"
        "  }"
        "}"
    )
    _run_checked(
        [
            "gh",
            "api",
            "graphql",
            "-F",
            f"id={pr_node_id}",
            "-F",
            "method=SQUASH",
            "-f",
            f"query={graphql}",
        ],
        cwd=_REPO_ROOT,
        timeout=30,
    )


def _run_coderabbit_triage(
    repo: str,
    pr_number: int,
    *,
    correlation_id: str,
    dry_run: bool,
) -> ModelCoderabbitTriageResult:
    handler = HandlerCoderabbitTriage()
    return handler.handle(
        ModelCoderabbitTriageCommand(
            repo=repo,
            pr_number=pr_number,
            correlation_id=correlation_id,
            dry_run=dry_run,
        )
    )


def _append_log_line(log_path: Path, line: str) -> None:
    with log_path.open("ab") as log_fh:
        log_fh.write((line + "\n").encode())


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
