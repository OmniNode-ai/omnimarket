# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live workflow runner for node_pr_polish.

The pure FSM in ``handler_pr_polish.py`` remains useful for unit coverage, but
the real branch-polishing path needs durable repair workflow evidence. This
module owns that live path:

- compile a bounded fixer worker using the dispatch-worker canary
- execute that spec through the dispatch-worker execution effect
- optionally publish the resulting delegation payload when Kafka is configured
- persist ``result.json`` and dispatch artifacts under the run directory
- run explicit-worktree local checks only when a worktree path is provided
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
    ModelCoderabbitTriageResult,
)
from omnimarket.nodes.node_dispatch_worker import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)
from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker_execution_effect import (
    ModelCompiledDispatchWorker,
    ModelDispatchWorkerDelegationPayload,
    ModelDispatchWorkerExecutionInput,
    ModelDispatchWorkerSpecArtifact,
)
from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
    HandlerDispatchWorkerExecution,
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


class RepairDispatchEvidence(TypedDict):
    """Persisted repair-worker dispatch artifacts for a PR polish run."""

    dispatch_worker_command_path: str
    dispatch_worker_result_path: str
    dispatch_worker_spec_path: str
    dispatch_execution_result_path: str
    delegation_payloads_path: str
    dispatch_receipt_dir: str
    repair_worker_payloads_prepared: int
    repair_workers_dispatched: int
    repair_workers_skipped: int
    repair_workers_rejected: int
    repair_workers_failed: int
    delegation_publish_status: str


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

        dispatch_evidence = _prepare_repair_worker_dispatch(
            command,
            run_dir=run_dir,
            started_at=started_at,
        )
        payload["repair_worker_dispatch"] = dispatch_evidence

        if not command.worktree_path:
            phase_results = _delegated_phase_results(command)
            payload["phase_results"] = phase_results
            payload["push_status"] = "deferred_to_repair_worker"
            payload["auto_merge_status"] = "deferred_to_repair_worker"
            completed = ModelPrPolishCompletedEvent(
                correlation_id=command.correlation_id,
                final_phase=EnumPrPolishPhase.DONE,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                pr_number=command.pr_number,
                run_dir=str(run_dir),
                dispatch_worker_spec_path=str(
                    dispatch_evidence["dispatch_worker_spec_path"]
                ),
                dispatch_execution_result_path=str(
                    dispatch_evidence["dispatch_execution_result_path"]
                ),
                delegation_payloads_path=str(
                    dispatch_evidence["delegation_payloads_path"]
                ),
                dispatch_receipt_dir=str(dispatch_evidence["dispatch_receipt_dir"]),
                repair_worker_payloads_prepared=int(
                    dispatch_evidence["repair_worker_payloads_prepared"]
                ),
                repair_workers_dispatched=int(
                    dispatch_evidence["repair_workers_dispatched"]
                ),
                repair_workers_skipped=int(dispatch_evidence["repair_workers_skipped"]),
                delegation_publish_status=str(
                    dispatch_evidence["delegation_publish_status"]
                ),
            )
            payload.update(
                {
                    "final_state": "COMPLETE",
                    "worktree_path": None,
                    "completed_at": completed.completed_at.isoformat(),
                }
            )
            payload["completed_event"] = completed.model_dump(mode="json")
            (run_dir / "result.json").write_text(json.dumps(payload, indent=2))
            return completed

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
            run_dir=str(run_dir),
            dispatch_worker_spec_path=str(
                dispatch_evidence["dispatch_worker_spec_path"]
            ),
            dispatch_execution_result_path=str(
                dispatch_evidence["dispatch_execution_result_path"]
            ),
            delegation_payloads_path=str(dispatch_evidence["delegation_payloads_path"]),
            dispatch_receipt_dir=str(dispatch_evidence["dispatch_receipt_dir"]),
            repair_worker_payloads_prepared=int(
                dispatch_evidence["repair_worker_payloads_prepared"]
            ),
            repair_workers_dispatched=int(
                dispatch_evidence["repair_workers_dispatched"]
            ),
            repair_workers_skipped=int(dispatch_evidence["repair_workers_skipped"]),
            delegation_publish_status=str(
                dispatch_evidence["delegation_publish_status"]
            ),
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
            run_dir=str(run_dir),
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
    raw_state_dir = os.environ.get("ONEX_STATE_DIR")
    state_dir = (
        Path(raw_state_dir)
        if raw_state_dir
        else _resolve_workspace_root() / ".onex_state"
    )
    if state_dir == Path("/.onex_state"):
        state_dir = _resolve_workspace_root() / ".onex_state"
    repo_slug = (command.repo or "unknown-repo").replace("/", "-")
    pr_part = str(command.pr_number) if command.pr_number is not None else "unknown-pr"
    run_id = uuid4().hex[:12]
    return state_dir / "pr-polish" / f"{repo_slug}-{pr_part}-{run_id}"


def _prepare_repair_worker_dispatch(
    command: ModelPrPolishStartCommand,
    *,
    run_dir: Path,
    started_at: datetime,
) -> RepairDispatchEvidence:
    if command.repo is None or command.pr_number is None:
        raise RuntimeError("repo and pr_number are required for repair dispatch")

    dispatch_dir = run_dir / "dispatch_worker"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    receipt_dir = run_dir / "dispatch_execution"
    ticket_id = _dispatch_ticket_id(command)
    dispatch_id = f"pr-polish-{_safe_segment(command.repo)}-{command.pr_number}"
    worker_name = dispatch_id[:64]
    command_model = ModelDispatchWorkerCommand(
        name=worker_name,
        team="omnimarket",
        role=EnumWorkerRole.fixer,
        scope=(
            f"Polish {command.repo}#{command.pr_number} toward merge readiness: "
            "inspect CI, review, and receipt blockers; create or reuse an Omni "
            "worktree; apply minimal fixes; run repo checks; push the PR branch; "
            "and leave durable evidence."
        ),
        targets=[
            ticket_id,
            f"{command.repo}#{command.pr_number}",
            command.repo,
        ],
        collision_fences=[],
        reports_to="pr-polish",
        wall_clock_cap_min=120,
        replace=True,
    )
    command_path = dispatch_dir / "dispatch_worker_command.json"
    command_path.write_text(command_model.model_dump_json(indent=2))

    tasks_dir = dispatch_dir / "tasks"
    (tasks_dir / command_model.team).mkdir(parents=True, exist_ok=True)
    with _dispatch_worker_environment():
        dispatch_result = HandlerDispatchWorker().handle(
            command_model,
            tasks_dir=tasks_dir,
            existing_task_subjects=[],
            state_dir=dispatch_dir / "records",
            parent_session_id=str(command.correlation_id),
        )
    result_path = dispatch_dir / "dispatch_worker_result.json"
    result_path.write_text(dispatch_result.model_dump_json(indent=2))
    if dispatch_result.rejected_reason:
        raise RuntimeError(
            f"repair worker dispatch rejected: {dispatch_result.rejected_reason}"
        )

    compiled = ModelCompiledDispatchWorker(
        validated_task_description=dispatch_result.validated_task_description,
        validated_prompt_template=dispatch_result.validated_prompt_template,
        proposed_agent_spawn_args=dispatch_result.proposed_agent_spawn_args,
        collision_fence_embeds=tuple(dispatch_result.collision_fence_embeds),
        rejected_reason=dispatch_result.rejected_reason,
    )
    artifact = ModelDispatchWorkerSpecArtifact(
        session_id=f"pr-polish-{command.correlation_id.hex[:12]}",
        ticket_id=ticket_id,
        dispatch_id=dispatch_id,
        correlation_chain=f"{command.correlation_id}.{dispatch_id}.{ticket_id}",
        compiled_at=started_at,
        dispatch_worker=compiled,
    )
    artifact_path = dispatch_dir / "dispatch_worker_spec.json"
    artifact_path.write_text(artifact.model_dump_json(indent=2))

    execution_result = HandlerDispatchWorkerExecution().handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=command.correlation_id,
            artifacts=(artifact,),
            state_dir=str(run_dir),
            receipt_dir=str(receipt_dir),
            dry_run=command.dry_run or command.no_push,
        )
    )
    execution_path = dispatch_dir / "dispatch_execution_result.json"
    execution_path.write_text(execution_result.model_dump_json(indent=2))
    payloads_path = dispatch_dir / "delegation_payloads.json"
    payloads_path.write_text(
        json.dumps(
            [
                payload.model_dump(mode="json")
                for payload in execution_result.delegation_payloads
            ],
            indent=2,
        )
    )

    publish_status, published_count = _publish_delegation_payloads(
        execution_result.delegation_payloads
    )
    return {
        "dispatch_worker_command_path": str(command_path),
        "dispatch_worker_result_path": str(result_path),
        "dispatch_worker_spec_path": str(artifact_path),
        "dispatch_execution_result_path": str(execution_path),
        "delegation_payloads_path": str(payloads_path),
        "dispatch_receipt_dir": str(receipt_dir),
        "repair_worker_payloads_prepared": execution_result.total_delegated,
        "repair_workers_dispatched": published_count,
        "repair_workers_skipped": execution_result.total_skipped,
        "repair_workers_rejected": execution_result.total_rejected,
        "repair_workers_failed": execution_result.total_failed,
        "delegation_publish_status": publish_status,
    }


def _delegated_phase_results(
    command: ModelPrPolishStartCommand,
) -> list[dict[str, object]]:
    dispatch_status = (
        "dry_run_dispatch_spec_compiled"
        if command.dry_run
        else "delegated_to_repair_worker"
    )
    if command.no_push and not command.dry_run:
        dispatch_status = "dispatch_spec_compiled_no_push"
    return [
        {"phase": "resolve_conflicts", "status": dispatch_status},
        {"phase": "fix_ci", "status": dispatch_status},
        {"phase": "address_comments", "status": dispatch_status},
        {
            "phase": "local_review",
            "status": dispatch_status,
            "detail": "Repair worker owns worktree creation, local checks, push, and evidence.",
        },
    ]


def _dispatch_ticket_id(command: ModelPrPolishStartCommand) -> str:
    if command.ticket_id and re.fullmatch(r"OMN-\d+", command.ticket_id):
        return command.ticket_id
    return f"OMN-{command.pr_number}"


def _safe_segment(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


@contextmanager
def _dispatch_worker_environment() -> Iterator[None]:
    updates: dict[str, str] = {}
    if not os.environ.get("OMNI_HOME"):
        workspace_root = _resolve_workspace_root()
        updates["OMNI_HOME"] = str(workspace_root)
    if not os.environ.get("OMNI_WORKTREES"):
        workspace_root = Path(updates.get("OMNI_HOME") or os.environ["OMNI_HOME"])
        updates["OMNI_WORKTREES"] = str(workspace_root / "omni_worktrees")

    old_values = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _resolve_workspace_root() -> Path:
    for parent in _REPO_ROOT.parents:
        if parent.name == "omni_worktrees":
            return parent.parent
    return _REPO_ROOT.parent


def _publish_delegation_payloads(
    payloads: Sequence[ModelDispatchWorkerDelegationPayload],
) -> tuple[str, int]:
    if not payloads:
        return "skipped_no_payloads", 0
    if os.environ.get("ONEX_PR_POLISH_DISABLE_DELEGATION_PUBLISH"):
        return "skipped_disabled", 0
    if not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
        return "skipped_no_kafka_bootstrap", 0

    async def _publish() -> int:
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

        bus = EventBusKafka.default()
        await bus.start()
        try:
            for payload in payloads:
                await bus.publish(
                    payload.topic,
                    key=None,
                    value=json.dumps(payload.payload).encode(),
                    headers=None,
                )
        finally:
            await bus.close()
        return len(payloads)

    published = asyncio.run(_publish())
    return f"published:{published}", published


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
                "status": "requires_github_required_checks",
                "detail": (
                    "Remote CI repair is not inferred locally; GitHub required checks "
                    "remain the merge proof."
                ),
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
        results.append(
            {
                "phase": "local_review",
                "status": "pre_commit_passed",
                "detail": "Ran uv run pre-commit run --all-files in the PR worktree.",
            }
        )

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
