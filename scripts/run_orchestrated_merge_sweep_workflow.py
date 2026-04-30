#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the merge-sweep workflow across compute, triage, effects, classify, reducer.

This is an operator proof surface for OMN-10400. It deliberately runs the same
typed node handlers that the bus uses and prints each node boundary inline.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun import (
    HandlerCiRerunEffect,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_merge_sweep_compute.__main__ import (
    _DEFAULT_REPOS,
    _load_failure_history,
    _to_pr_info,
)
from omnimarket.nodes.node_merge_sweep_compute.adapter_github_http import (
    GitHubHttpClient,
)
from omnimarket.nodes.node_merge_sweep_compute.branch_protection import (
    BranchProtectionCache,
)
from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelMergeSweepRequest,
    ModelMergeSweepResult,
    ModelPRInfo,
    NodeMergeSweep,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage import (
    HandlerTriageOrchestrator,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
    ModelCiRerunCommand,
    ModelRebaseCommand,
    ModelTriageRequest,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_completed_event import (
    ModelPrPolishCompletedEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.workflow_runner import run_live_pr_polish
from omnimarket.nodes.node_rebase_effect.handlers.handler_rebase import (
    HandlerRebaseEffect,
)
from omnimarket.nodes.node_sweep_outcome_classify.handlers.handler_outcome_classify import (
    HandlerSweepOutcomeClassify,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcomeEventType,
    ModelSweepOutcomeClassified,
    ModelSweepOutcomeInput,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_state_dir() -> str:
    configured = os.environ.get("ONEX_STATE_DIR")
    if configured:
        path = Path(configured).expanduser()
        if path.exists() or (path.parent.exists() and os.access(path.parent, os.W_OK)):
            return str(path)
    return str(Path.home() / ".onex_state")


def _log(node: str, message: str) -> None:
    print(f"[{node}] {message}", flush=True)


def _dump_model(value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _run(argv: list[str], *, cwd: Path = REPO_ROOT, timeout: int = 120) -> str:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        first_line = detail[0] if detail else "no output"
        raise RuntimeError(
            f"{' '.join(argv)} failed with exit {proc.returncode}: {first_line}"
        )
    return proc.stdout.strip()


def _load_open_prs(repos: Iterable[str]) -> list[ModelPRInfo]:
    github = GitHubHttpClient()
    protection = BranchProtectionCache(github)
    prs: list[ModelPRInfo] = []
    for repo in repos:
        required_approving = protection.required_approving_review_count(repo)
        repo_prs = list(github.fetch_open_prs(repo))
        _log(
            "node_merge_sweep_compute",
            f"fetched_open_prs repo={repo} count={len(repo_prs)} "
            f"required_approving_review_count={required_approving}",
        )
        prs.extend(_to_pr_info(pr, repo, required_approving) for pr in repo_prs)
    return prs


def _classify_open_prs(
    prs: list[ModelPRInfo],
    *,
    require_approval: bool,
    merge_method: str,
    max_total_merges: int,
    skip_polish: bool,
    state_dir: Path,
    use_lifecycle_ordering: bool,
) -> ModelMergeSweepResult:
    request = ModelMergeSweepRequest(
        prs=prs,
        require_approval=require_approval,
        merge_method=merge_method,
        max_total_merges=max_total_merges,
        skip_polish=skip_polish,
        failure_history=_load_failure_history(str(state_dir)),
        use_lifecycle_ordering=use_lifecycle_ordering,
    )
    return NodeMergeSweep().handle(request)


async def _triage(
    classification: ModelMergeSweepResult,
    *,
    run_id: Any,
    correlation_id: Any,
    execute: bool,
) -> tuple[Any, ...]:
    request = ModelTriageRequest(
        classification=classification,
        run_id=run_id,
        correlation_id=correlation_id,
        total_prs=len(classification.classified),
        emit_pr_polish_commands=True,
        dry_run=not execute,
    )
    output = await HandlerTriageOrchestrator().handle(request)
    return tuple(output.events)


def _pr_head(repo: str, pr_number: int) -> tuple[str, str]:
    raw = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName,headRefOid",
        ],
        timeout=30,
    )
    payload = json.loads(raw)
    branch = str(payload.get("headRefName") or "").strip()
    sha = str(payload.get("headRefOid") or "").strip()
    if not branch:
        raise RuntimeError(f"empty headRefName for {repo}#{pr_number}")
    if not sha:
        raise RuntimeError(f"empty headRefOid for {repo}#{pr_number}")
    return branch, sha


def _worktrees_for_branch(branch: str) -> list[Path]:
    raw = _run(["git", "worktree", "list", "--porcelain"], timeout=15)
    paths: list[Path] = []
    for block in raw.split("\n\n"):
        raw_path = ""
        raw_branch = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                raw_path = line[9:].strip()
            elif line.startswith("branch "):
                raw_branch = line[7:].strip()
        if raw_path and raw_branch.endswith(f"/{branch}"):
            paths.append(Path(raw_path))
    return paths


def _worktree_head_sha(worktree: Path) -> str | None:
    try:
        return _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=15)
    except RuntimeError:
        return None


def _is_clean_worktree(worktree: Path) -> bool:
    status = _run(["git", "-C", str(worktree), "status", "--short"], timeout=30)
    return not status


def _prepare_polish_worktree(
    repo: str, pr_number: int
) -> tuple[Path, bool, str | None]:
    branch, head_sha = _pr_head(repo, pr_number)

    current_sha = _worktree_head_sha(REPO_ROOT)
    if current_sha == head_sha:
        if not _is_clean_worktree(REPO_ROOT):
            raise RuntimeError(
                f"refusing to polish {repo}#{pr_number}: current PR-head worktree "
                f"is dirty ({REPO_ROOT})"
            )
        return REPO_ROOT, False, None

    dirty_candidates: list[Path] = []
    for existing in _worktrees_for_branch(branch):
        if _is_clean_worktree(existing):
            return existing, False, None
        dirty_candidates.append(existing)
    if dirty_candidates:
        _log(
            "node_pr_polish",
            "ignoring_dirty_branch_worktrees "
            + ",".join(str(path) for path in dirty_candidates),
        )

    tmp_root = Path(tempfile.mkdtemp(prefix=f"omni-pr-{pr_number}-"))
    worktree = tmp_root / "worktree"
    created_branch = f"omni-pr-{pr_number}-{uuid4().hex[:8]}"
    _run(
        ["git", "fetch", "origin", f"pull/{pr_number}/head:{created_branch}"],
        timeout=120,
    )
    _run(["git", "worktree", "add", str(worktree), created_branch], timeout=120)
    return worktree, True, created_branch


def _cleanup_polish_worktree(
    worktree: Path,
    *,
    created_by_script: bool,
    created_branch: str | None,
    keep_worktrees: bool,
) -> None:
    if keep_worktrees or not created_by_script:
        return
    status = _run(["git", "-C", str(worktree), "status", "--short"], timeout=30)
    if status:
        _log(
            "node_pr_polish",
            f"retained_dirty_worktree path={worktree} status={status!r}",
        )
        return
    _run(["git", "worktree", "remove", "--force", str(worktree)], timeout=120)
    if created_branch:
        _run(["git", "branch", "-D", created_branch], timeout=30)
    with contextlib.suppress(OSError):
        worktree.parent.rmdir()


def _polish_completed_to_input(
    command: ModelPrPolishStartCommand,
    completed: ModelPrPolishCompletedEvent,
    *,
    run_id: Any,
    total_prs: int,
) -> ModelSweepOutcomeInput:
    return ModelSweepOutcomeInput(
        event_type=EnumSweepOutcomeEventType.PR_POLISH_COMPLETED,
        pr_number=command.pr_number or 0,
        repo=command.repo or "",
        correlation_id=command.correlation_id,
        run_id=run_id,
        total_prs=total_prs,
        error=completed.error_message,
        extra={"final_phase": completed.final_phase.value},
    )


def _effect_event_to_input(event: Any) -> ModelSweepOutcomeInput:
    payload = event.model_dump(mode="python")
    if "armed" in payload:
        return ModelSweepOutcomeInput(
            event_type=EnumSweepOutcomeEventType.ARMED,
            pr_number=event.pr_number,
            repo=event.repo,
            correlation_id=event.correlation_id,
            run_id=event.run_id,
            total_prs=event.total_prs,
            armed=event.armed,
            error=event.error,
        )
    if "rerun_triggered" in payload:
        return ModelSweepOutcomeInput(
            event_type=EnumSweepOutcomeEventType.CI_RERUN_TRIGGERED,
            pr_number=event.pr_number,
            repo=event.repo,
            correlation_id=event.correlation_id,
            run_id=event.run_id,
            total_prs=event.total_prs,
            rerun_triggered=event.rerun_triggered,
            error=event.error,
        )
    if "success" in payload and "conflict_files" in payload:
        return ModelSweepOutcomeInput(
            event_type=EnumSweepOutcomeEventType.REBASE_COMPLETED,
            pr_number=event.pr_number,
            repo=event.repo,
            correlation_id=event.correlation_id,
            run_id=event.run_id,
            total_prs=event.total_prs,
            success=event.success,
            conflict_files=event.conflict_files,
            error=event.error,
        )
    raise TypeError(f"unsupported effect event: {type(event).__name__}")


def _classify_outcome(request: ModelSweepOutcomeInput) -> ModelSweepOutcomeClassified:
    output = HandlerSweepOutcomeClassify().handle(request)
    if output.result is None:
        raise RuntimeError("node_sweep_outcome_classify returned no result")
    result = output.result
    if not isinstance(result, ModelSweepOutcomeClassified):
        raise TypeError(f"unexpected outcome type: {type(result).__name__}")
    _log(
        "node_sweep_outcome_classify",
        f"{result.repo}#{result.pr_number} source={result.source_event_type} "
        f"outcome={result.outcome.value}",
    )
    return result


async def _execute_command(
    command: Any,
    *,
    execute: bool,
    run_id: Any,
    state_dir: Path,
    total_prs: int,
    keep_worktrees: bool,
) -> list[ModelSweepOutcomeClassified]:
    if isinstance(command, ModelPrPolishStartCommand):
        worktree, created_by_script, created_branch = _prepare_polish_worktree(
            command.repo or "", command.pr_number or 0
        )
        run_dir = state_dir / "merge-sweep-workflow" / str(command.correlation_id)
        command = command.model_copy(
            update={"worktree_path": str(worktree), "run_dir": str(run_dir)}
        )
        _log(
            "node_pr_polish",
            f"executing repo={command.repo} pr={command.pr_number} "
            f"dry_run={command.dry_run} no_push={command.no_push} "
            f"no_automerge={command.no_automerge} worktree={worktree}",
        )
        try:
            completed = run_live_pr_polish(command)
        finally:
            _cleanup_polish_worktree(
                worktree,
                created_by_script=created_by_script,
                created_branch=created_branch,
                keep_worktrees=keep_worktrees,
            )
        _log("node_pr_polish", _dump_model(completed))
        # PR polish is verification evidence for Track B. The Phase 1 reducer
        # still needs the merge-sweep effect outcome for the PR, such as CI rerun.
        outcome = _classify_outcome(
            _polish_completed_to_input(
                command, completed, run_id=run_id, total_prs=total_prs
            )
        )
        return [outcome]

    if not execute:
        _log(type(command).__name__, f"planned {_dump_model(command)}")
        return []

    if isinstance(command, ModelAutoMergeArmCommand):
        _log(
            "node_merge_sweep_auto_merge_arm_effect",
            f"executing {command.repo}#{command.pr_number}",
        )
        output = await HandlerAutoMergeArmEffect().handle(command)
    elif isinstance(command, ModelCiRerunCommand):
        _log(
            "node_ci_rerun_effect",
            f"executing {command.repo}#{command.pr_number} run={command.run_id_github}",
        )
        output = await HandlerCiRerunEffect().handle(command)
    elif isinstance(command, ModelRebaseCommand):
        _log(
            "node_rebase_effect",
            f"executing {command.repo}#{command.pr_number} head={command.head_ref_name}",
        )
        output = await HandlerRebaseEffect().handle(command)
    else:
        _log(type(command).__name__, f"no live effect wired {_dump_model(command)}")
        return []

    outcomes = []
    for event in output.events:
        _log(output.handler_id, _dump_model(event))
        outcomes.append(_classify_outcome(_effect_event_to_input(event)))
    return outcomes


async def _run_workflow(args: argparse.Namespace) -> int:
    run_id = uuid4()
    correlation_id = uuid4()
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS
    state_dir = Path(args.state_dir).expanduser()

    _log(
        "workflow",
        f"mode={'execute' if args.execute else 'dry-run'} run_id={run_id} "
        f"correlation_id={correlation_id}",
    )
    prs = _load_open_prs(repos)
    classification = _classify_open_prs(
        prs,
        require_approval=args.require_approval,
        merge_method=args.merge_method,
        max_total_merges=args.max_total_merges,
        skip_polish=args.skip_polish,
        state_dir=state_dir,
        use_lifecycle_ordering=args.use_lifecycle_ordering,
    )
    _log(
        "node_merge_sweep_compute",
        f"status={classification.status} classified={len(classification.classified)}",
    )
    for item in classification.classified:
        _log(
            "node_merge_sweep_compute",
            f"PR {item.pr.number} track={item.track.value} reason={item.reason}",
        )

    events = await _triage(
        classification,
        run_id=run_id,
        correlation_id=correlation_id,
        execute=args.execute,
    )
    _log("node_merge_sweep_triage_orchestrator", f"emitted_events={len(events)}")
    for event in events:
        _log("node_merge_sweep_triage_orchestrator", _dump_model(event))

    reducer = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(
        run_id=run_id,
        total_prs=len({(event.repo, event.pr_number) for event in events}),
    )
    _log(
        "node_merge_sweep_state_reducer",
        f"reducing_total_prs={state.total_prs}",
    )
    for command in events:
        outcomes = await _execute_command(
            command,
            execute=args.execute,
            run_id=run_id,
            state_dir=state_dir,
            total_prs=state.total_prs,
            keep_worktrees=args.keep_worktrees,
        )
        for outcome in outcomes:
            state, intents = reducer.delta(state, outcome)
            _log(
                "node_merge_sweep_state_reducer",
                f"{outcome.repo}#{outcome.pr_number} outcome={outcome.outcome.value} "
                f"terminal_intents={sum(isinstance(i, dict) for i in intents)}",
            )
            for intent in intents:
                if isinstance(intent, dict):
                    _log(
                        "node_merge_sweep_state_reducer",
                        "terminal_intent "
                        f"topic={intent['topic']} payload={_dump_model(intent['payload'])}",
                    )

    _log(
        "summary",
        "total_prs="
        f"{state.total_prs} recorded={len(state.pr_outcomes_by_key)} "
        f"armed={state.armed_count} rebased={state.rebased_count} "
        f"ci_rerun={state.ci_rerun_count} failed={state.failed_count} "
        f"stuck={state.stuck_count} terminal_emitted={state.terminal_emitted}",
    )
    _log("summary", f"outcome_keys={sorted(state.pr_outcomes_by_key)}")
    return 0 if state.failed_count == 0 and state.stuck_count == 0 else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run merge-sweep compute -> triage -> effects -> classify -> reducer."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated org/repo names (default: all OmniNode repos).",
    )
    parser.add_argument("--execute", action="store_true", help="Run live effects.")
    parser.add_argument(
        "--require-approval",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--merge-method",
        default="squash",
        choices=["squash", "merge", "rebase"],
    )
    parser.add_argument("--max-total-merges", type=int, default=0)
    parser.add_argument("--skip-polish", action="store_true", default=False)
    parser.add_argument("--use-lifecycle-ordering", action="store_true", default=False)
    parser.add_argument(
        "--state-dir",
        default=_default_state_dir(),
    )
    parser.add_argument("--keep-worktrees", action="store_true", default=False)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(_run_workflow(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
