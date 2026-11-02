# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paired CLAUDE.md / AGENTS.md on-off benchmark orchestrator (OMN-21896).

Every task runs twice on a copy of the same source tree: arm ``on`` keeps
CLAUDE.md and AGENTS.md, arm ``off`` has every file of those names removed from
the copy (never from the source). The task's own check decides the outcome.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
    CommandFn,
    LaneClaudeHeadless,
    LaneCodexHeadless,
    LaneJevTypedDecision,
    ProtocolBenchmarkLane,
    apply_file_blocks,
    run_command,
    scrub,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.report_rows import (
    build_rows,
    render_markdown,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    EnumBenchmarkArm,
    EnumBenchmarkLane,
    EnumRunOutcome,
    ModelBenchmarkLaneSpec,
    ModelBenchmarkTask,
    ModelContextBenchmarkRequest,
    ModelContextBenchmarkResult,
    ModelLaneInvocation,
    ModelRunRecord,
)

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "node_modules"
)


def tree_sha(source: Path) -> str:
    """Content hash of the tree the arms are copied from (the same for both arms)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = path.relative_to(source)
        if any(part in {".git", "__pycache__", ".venv"} for part in rel.parts):
            continue
        digest.update(str(rel).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def prepare_arm(
    source: Path, dest: Path, arm: EnumBenchmarkArm, names: tuple[str, ...]
) -> str | None:
    """Copy ``source`` to ``dest``; the off arm loses every context file.

    Returns an error string when the on arm lacks a context file, because an
    "on" arm with nothing present measures nothing.
    """
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=_COPY_IGNORE)
    if arm is EnumBenchmarkArm.OFF:
        for path in [p for p in dest.rglob("*") if p.is_file() and p.name in names]:
            path.unlink()
        return None
    missing = [n for n in names if not (dest / n).is_file()]
    return f"context_file_missing:{','.join(missing)}" if missing else None


def read_context(workdir: Path, names: tuple[str, ...]) -> str:
    return "\n\n".join(
        f"# {name}\n{(workdir / name).read_text(encoding='utf-8')}"
        for name in names
        if (workdir / name).is_file()
    )


class HandlerContextBenchmark:
    """Runs the paired benchmark and builds the per-model, per-condition report."""

    def __init__(
        self,
        *,
        runners: Mapping[EnumBenchmarkLane, ProtocolBenchmarkLane] | None = None,
        command_fn: CommandFn = run_command,
    ) -> None:
        self._command_fn = command_fn
        defaults: dict[EnumBenchmarkLane, ProtocolBenchmarkLane] = {
            EnumBenchmarkLane.CLAUDE_HEADLESS: LaneClaudeHeadless(command_fn),
            EnumBenchmarkLane.CODEX_HEADLESS: LaneCodexHeadless(command_fn),
            EnumBenchmarkLane.JEV_TYPED_DECISION: LaneJevTypedDecision(
                command_fn=command_fn
            ),
        }
        self._runners = {**defaults, **(runners or {})}

    def handle(
        self, request: ModelContextBenchmarkRequest
    ) -> ModelContextBenchmarkResult:
        source = Path(request.source_dir)
        sha = tree_sha(source)
        host = request.host if request.host != "unknown" else socket.gethostname()
        with ThreadPoolExecutor(max_workers=request.max_parallel_lanes) as pool:
            lane_results = list(
                pool.map(
                    lambda spec: self._run_lane(request, spec, source, sha, host),
                    request.lanes,
                )
            )
        runs = tuple(r for records, _ in lane_results for r in records)
        blocked = tuple(b for _, b in lane_results if b)
        rows = build_rows(runs, min_paired_tasks=request.min_paired_tasks)
        return ModelContextBenchmarkResult(
            source_sha=sha,
            host=host,
            runs=runs,
            rows=rows,
            blocked_lanes=blocked,
            report_markdown=render_markdown(
                rows, min_paired_tasks=request.min_paired_tasks
            ),
        )

    def _run_lane(
        self,
        request: ModelContextBenchmarkRequest,
        spec: ModelBenchmarkLaneSpec,
        source: Path,
        sha: str,
        host: str,
    ) -> tuple[list[ModelRunRecord], str]:
        runner = self._runners.get(spec.lane)
        records: list[ModelRunRecord] = []
        blocked = ""
        for index, task in enumerate(request.tasks):
            arms = (
                (EnumBenchmarkArm.ON, EnumBenchmarkArm.OFF)
                if index % 2 == 0
                else (EnumBenchmarkArm.OFF, EnumBenchmarkArm.ON)
            )
            for arm in arms:
                if blocked:
                    records.append(
                        _record(
                            task, spec, arm, sha, EnumRunOutcome.BLOCKED, detail=blocked
                        )
                    )
                    continue
                if runner is None:
                    records.append(
                        _record(
                            task,
                            spec,
                            arm,
                            sha,
                            EnumRunOutcome.BLOCKED,
                            detail=f"no runner bound for lane {spec.lane.value}",
                        )
                    )
                    continue
                record = self._run_one(request, spec, runner, task, arm, source, sha)
                if record.outcome is EnumRunOutcome.BLOCKED:
                    blocked = f"blocked:{record.detail} host={host}"
                    record = record.model_copy(update={"detail": blocked})
                records.append(record)
        return records, (f"{spec.model_id}: {blocked}" if blocked else "")

    def _run_one(
        self,
        request: ModelContextBenchmarkRequest,
        spec: ModelBenchmarkLaneSpec,
        runner: ProtocolBenchmarkLane,
        task: ModelBenchmarkTask,
        arm: EnumBenchmarkArm,
        source: Path,
        sha: str,
    ) -> ModelRunRecord:
        started = monotonic()
        workdir = (
            Path(request.scratch_dir)
            / spec.lane.value
            / spec.model_id.replace("/", "_")
            / task.task_id
            / arm.value
        )
        error = prepare_arm(source, workdir, arm, request.context_file_names)
        if error:
            return _record(task, spec, arm, sha, EnumRunOutcome.ERROR, detail=error)
        context = (
            read_context(workdir, request.context_file_names)
            if arm is EnumBenchmarkArm.ON
            else None
        )
        invocation = runner.invoke(
            spec=spec,
            task=task,
            workdir=workdir,
            context_text=context,
            timeout=request.agent_timeout_seconds,
        )
        duration = monotonic() - started
        if invocation.blocked:
            return _record(
                task,
                spec,
                arm,
                sha,
                EnumRunOutcome.BLOCKED,
                invocation,
                invocation.blocked,
                duration,
            )
        if invocation.not_applicable:
            return _record(
                task,
                spec,
                arm,
                sha,
                EnumRunOutcome.NOT_APPLICABLE,
                invocation,
                scrub(invocation.not_applicable),
                duration,
            )
        if invocation.error:
            return _record(
                task,
                spec,
                arm,
                sha,
                EnumRunOutcome.ERROR,
                invocation,
                scrub(invocation.error),
                duration,
            )
        if invocation.check_passed is not None:
            outcome = (
                EnumRunOutcome.PASS if invocation.check_passed else EnumRunOutcome.FAIL
            )
            return _record(
                task,
                spec,
                arm,
                sha,
                outcome,
                invocation,
                "typed-decision answer",
                duration,
            )
        if spec.lane is EnumBenchmarkLane.DELEGATE_ROUTE:
            written = apply_file_blocks(invocation.output_text, task, workdir)
            if written == 0:
                return _record(
                    task,
                    spec,
                    arm,
                    sha,
                    EnumRunOutcome.FAIL,
                    invocation,
                    "model returned no file block for an edit file",
                    duration,
                )
        for rel in task.protected_paths:
            _restore(source / rel, workdir / rel)
        argv = tuple(a.replace("{python}", _python()) for a in task.check_argv)
        check = self._command_fn(
            argv, workdir, request.check_timeout_seconds, None, None
        )
        outcome = EnumRunOutcome.PASS if check.returncode == 0 else EnumRunOutcome.FAIL
        detail = f"check rc={check.returncode}" + (
            " timeout" if check.timed_out else ""
        )
        return _record(task, spec, arm, sha, outcome, invocation, detail, duration)


def _python() -> str:
    import sys

    return sys.executable


def _restore(src: Path, dst: Path) -> None:
    if dst.is_dir():
        shutil.rmtree(dst)
    elif dst.exists():
        dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, ignore=_COPY_IGNORE)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _record(
    task: ModelBenchmarkTask,
    spec: ModelBenchmarkLaneSpec,
    arm: EnumBenchmarkArm,
    sha: str,
    outcome: EnumRunOutcome,
    invocation: ModelLaneInvocation | None = None,
    detail: str = "",
    duration: float = 0.0,
) -> ModelRunRecord:
    inv = invocation or ModelLaneInvocation()
    return ModelRunRecord(
        task_id=task.task_id,
        lane=spec.lane,
        model_id=spec.model_id,
        arm=arm,
        source_sha=sha,
        outcome=outcome,
        input_tokens=inv.input_tokens,
        output_tokens=inv.output_tokens,
        cache_read_tokens=inv.cache_read_tokens,
        cache_write_tokens=inv.cache_write_tokens,
        served_model=inv.served_model,
        duration_seconds=round(duration, 2),
        detail=scrub(detail),
    )
