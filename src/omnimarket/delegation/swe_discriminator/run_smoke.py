#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the 2x2 SWE-discriminator SMOKE battery (OMN-13988).

Two phases, deliberately separated (verifier != runner):

  1. RUN  — for every (task, arm) cell, execute the arm live ``k`` times and
            capture each artifact + all model calls to ``arm_runs.json``.
  2. GRADE — a separate pass loads the captured artifacts (arm identity stripped
            at grade time), runs the deterministic hard floor, classifies each
            run (pass / fail_wrong / truncated / blocked / no_artifact), and
            aggregates pass^k per cell. Writes ``graded_rows.json`` +
            ``smoke_report.json``.

The battery answers: does the pipeline emit usable, gradeable rows end-to-end,
or collapse to zero like OMN-12792? Truncated/blocked runs are recorded but
EXCLUDED from capability scoring so the L3/L4 numbers do not lie.

    uv run python -m omnimarket.delegation.swe_discriminator.run_smoke \\
        --out-dir docs/evidence/2026-07-05-swe-discriminator-smoke --k 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.delegation.swe_discriminator.arm_runner import run_arm
from omnimarket.delegation.swe_discriminator.classify import (
    classify_run,
    detect_truncation,
)
from omnimarket.delegation.swe_discriminator.corpus import (
    DEFAULT_CORPUS_PATH,
    load_corpus,
)
from omnimarket.delegation.swe_discriminator.grader import grade_floor
from omnimarket.delegation.swe_discriminator.model_client import resolve_tier
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumArm,
    EnumRouting,
    EnumRunOutcome,
    GradedRow,
    PassKCell,
    SmokeReport,
    SweTask,
)


def _run_phase(
    tasks: list[SweTask], arms: list[EnumArm], k: int
) -> list[tuple[int, ArmRun]]:
    runs: list[tuple[int, ArmRun]] = []
    for task in tasks:
        for arm in arms:
            for rep in range(k):
                print(f"  RUN  {task.task_id} :: {arm.value} [rep {rep}]", flush=True)
                run = run_arm(task, arm)
                print(
                    f"       slices={run.n_slices} artifact_chars={len(run.artifact)} "
                    f"cost=${run.total_cost_usd:.6f} "
                    f"{('ERR: ' + run.error) if run.error else 'ok'}",
                    flush=True,
                )
                runs.append((rep, run))
    return runs


def _grade_phase(
    tasks: list[SweTask], runs: list[tuple[int, ArmRun]]
) -> list[GradedRow]:
    task_by_id = {t.task_id: t for t in tasks}
    rows: list[GradedRow] = []
    for rep, run in runs:
        task = task_by_id[run.task_id]
        artifact_produced = bool(run.artifact.strip())
        floor_passed, floor_detail = (
            grade_floor(task, run.artifact) if artifact_produced else (False, "")
        )
        run.truncated = detect_truncation(task, run)
        outcome = classify_run(task, run, floor_passed=floor_passed)
        if not floor_detail:
            floor_detail = f"{outcome.value}: {run.error or 'no artifact'}"
        usable = outcome.is_capability_signal
        print(
            f"  GRADE {run.task_id} :: {run.arm.value} [rep {rep}] -> "
            f"{outcome.value} usable={usable} :: {floor_detail}",
            flush=True,
        )
        rows.append(
            GradedRow(
                task_id=run.task_id,
                level=task.level,
                arm=run.arm,
                decomposition=run.decomposition,
                routing=run.routing,
                repeat=rep,
                outcome=outcome,
                artifact_produced=artifact_produced,
                artifact_chars=len(run.artifact),
                floor_passed=floor_passed,
                floor_detail=floor_detail,
                usable=usable,
                truncated=run.truncated,
                total_cost_usd=run.total_cost_usd,
                decomposition_tax_usd=run.decomposition_tax_usd,
                total_latency_ms=run.total_latency_ms,
                n_slices=run.n_slices,
                blocked=run.blocked,
                error=run.error,
            )
        )
    return rows


def _aggregate_cells(
    tasks: list[SweTask], arms: list[EnumArm], rows: list[GradedRow], k: int
) -> list[PassKCell]:
    cells: list[PassKCell] = []
    for task in tasks:
        for arm in arms:
            cell_rows = [r for r in rows if r.task_id == task.task_id and r.arm == arm]
            scored = [r for r in cell_rows if r.outcome.is_capability_signal]
            excluded = [r for r in cell_rows if r.outcome.excluded_from_scoring]
            passes = sum(1 for r in scored if r.outcome is EnumRunOutcome.PASS)
            mean_cost = (
                round(sum(r.total_cost_usd for r in cell_rows) / len(cell_rows), 8)
                if cell_rows
                else 0.0
            )
            cells.append(
                PassKCell(
                    task_id=task.task_id,
                    level=task.level,
                    arm=arm,
                    k=k,
                    scored_repeats=len(scored),
                    passes=passes,
                    excluded_repeats=len(excluded),
                    # pass^k = passed ALL scored repeats (and at least one scored).
                    pass_hat_k=bool(scored) and passes == len(scored),
                    pass_at_1=passes >= 1,
                    outcomes=[r.outcome for r in cell_rows],
                    mean_cost_usd=mean_cost,
                )
            )
    return cells


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--k", type=int, default=1, help="repeats per cell (pass^k)")
    parser.add_argument(
        "--arms",
        nargs="*",
        default=[a.value for a in EnumArm],
        help="arm ids to run (default: all four)",
    )
    parser.add_argument(
        "--task", action="append", default=[], help="restrict to these task_ids"
    )
    args = parser.parse_args(argv)

    tasks = load_corpus(args.corpus)
    if args.task:
        tasks = [t for t in tasks if t.task_id in args.task]
    arms = [EnumArm(a) for a in args.arms]
    k = max(1, args.k)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Fail fast if a tier is unreachable BEFORE running — a silent unreachable
    # tier would look like a capability failure.
    _, frontier_model, _, _, _ = resolve_tier(EnumRouting.FRONTIER)
    _, cost_model, _, _, _ = resolve_tier(EnumRouting.COST_ROUTED)
    print(f"frontier={frontier_model} cost_routed={cost_model} k={k}")
    print(f"tasks={len(tasks)} arms={len(arms)} cells={len(tasks) * len(arms)}\n")

    print("=== PHASE 1: RUN (capture artifacts) ===")
    runs = _run_phase(tasks, arms, k)
    (args.out_dir / "arm_runs.json").write_text(
        json.dumps([r.model_dump(mode="json") for _, r in runs], indent=2) + "\n"
    )

    print("\n=== PHASE 2: GRADE (offline, blind, verifier != runner) ===")
    rows = _grade_phase(tasks, runs)
    (args.out_dir / "graded_rows.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in rows], indent=2) + "\n"
    )
    cells = _aggregate_cells(tasks, arms, rows, k)

    usable = sum(1 for r in rows if r.usable)
    truncated = sum(1 for r in rows if r.outcome is EnumRunOutcome.TRUNCATED)
    blocked = sum(1 for r in rows if r.outcome is EnumRunOutcome.BLOCKED)
    report = SmokeReport(
        recorded_at=datetime.now(UTC).isoformat(),
        n_tasks=len(tasks),
        n_arms=len(arms),
        k=k,
        rows=rows,
        cells=cells,
        usable_rows=usable,
        truncated_rows=truncated,
        blocked_rows=blocked,
        total_rows=len(rows),
        zero_usable_rows=usable == 0,
        frontier_model=frontier_model,
        cost_routed_model=cost_model,
        notes=[
            "proof_class = offline over captured artifacts (NOT a live runtime loop)",
            "task-scope = function/module-slice reconstruction, not full-repo checkout",
            "deterministic hard floor only; no LLM-judge in the smoke",
            "decomposed-arm integration = deterministic concat (labeled)",
            "TRUNCATED/BLOCKED runs excluded from capability scoring (pass^k denom)",
        ],
    )
    (args.out_dir / "smoke_report.json").write_text(
        report.model_dump_json(indent=2) + "\n"
    )

    print(
        f"\n=== SMOKE RESULT: {usable}/{len(rows)} usable rows "
        f"({'ZERO — collapse (OMN-12792 mode)' if usable == 0 else 'pipeline emits rows'}) "
        f"| truncated={truncated} blocked={blocked} ==="
    )
    for c in cells:
        verdict = (
            "no-signal"
            if c.scored_repeats == 0
            else f"pass^{c.k}={'Y' if c.pass_hat_k else 'N'} "
            f"({c.passes}/{c.scored_repeats} scored, {c.excluded_repeats} excl)"
        )
        print(f"  L{c.level} {c.task_id:32s} {c.arm.value:26s} {verdict}")
    print(f"\nartifacts written under {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
