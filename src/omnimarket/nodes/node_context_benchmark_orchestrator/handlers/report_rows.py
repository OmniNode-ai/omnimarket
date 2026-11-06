# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-model, per-condition report with the paired on/off difference (OMN-21896).

Pure: runs in, rows out. A model with fewer than ``min_paired_tasks`` paired
tasks is underpowered and carries no verdict.
"""

from __future__ import annotations

from collections import defaultdict
from math import comb
from statistics import fmean

from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    UNKNOWN,
    EnumBenchmarkArm,
    EnumBenchmarkLane,
    EnumRunOutcome,
    EnumVerdict,
    ModelReportRow,
    ModelRunRecord,
)

_ALPHA = 0.05
_CHECKED = (EnumRunOutcome.PASS, EnumRunOutcome.FAIL)


def total_tokens(run: ModelRunRecord) -> int | None:
    """Input + output + cache read + cache write; None when any is UNKNOWN."""
    parts = (
        run.input_tokens,
        run.output_tokens,
        run.cache_read_tokens,
        run.cache_write_tokens,
    )
    if all(isinstance(p, int) for p in parts):
        return sum(p for p in parts if isinstance(p, int))
    return None


def fresh_tokens(run: ModelRunRecord) -> int | None:
    """Input + output, the tokens that are not served from cache."""
    if isinstance(run.input_tokens, int) and isinstance(run.output_tokens, int):
        return run.input_tokens + run.output_tokens
    return None


def sign_test_p(on_only: int, off_only: int) -> float:
    """Exact two-sided sign test over the discordant pairs."""
    n = on_only + off_only
    if n == 0:
        return 1.0
    k = min(on_only, off_only)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def _mean(values: list[int]) -> float | str:
    return fmean(values) if values else UNKNOWN


def build_rows(
    runs: tuple[ModelRunRecord, ...], *, min_paired_tasks: int
) -> tuple[ModelReportRow, ...]:
    groups: dict[tuple[str, EnumBenchmarkLane], list[ModelRunRecord]] = defaultdict(
        list
    )
    for run in runs:
        groups[(run.model_id, run.lane)].append(run)

    rows: list[ModelReportRow] = []
    for (model_id, lane), group in sorted(groups.items(), key=lambda g: g[0][0]):
        by_arm = {
            arm: {r.task_id: r for r in group if r.arm is arm}
            for arm in EnumBenchmarkArm
        }
        on_runs, off_runs = by_arm[EnumBenchmarkArm.ON], by_arm[EnumBenchmarkArm.OFF]
        pairs = [
            (on_runs[t], off_runs[t])
            for t in sorted(on_runs.keys() & off_runs.keys())
            if on_runs[t].outcome in _CHECKED and off_runs[t].outcome in _CHECKED
        ]
        on_only = sum(
            1
            for on, off in pairs
            if on.outcome is EnumRunOutcome.PASS and off.outcome is EnumRunOutcome.FAIL
        )
        off_only = sum(
            1
            for on, off in pairs
            if off.outcome is EnumRunOutcome.PASS and on.outcome is EnumRunOutcome.FAIL
        )
        rate_diff: float | None = None
        token_diff: float | str | None = None
        if pairs:
            rate_diff = fmean(
                (on.outcome is EnumRunOutcome.PASS)
                - (off.outcome is EnumRunOutcome.PASS)
                for on, off in pairs
            )
            token_pairs = [
                (a, b)
                for on, off in pairs
                if (a := total_tokens(on)) is not None
                and (b := total_tokens(off)) is not None
            ]
            token_diff = (
                fmean(a - b for a, b in token_pairs) if token_pairs else UNKNOWN
            )
        underpowered = len(pairs) < min_paired_tasks
        p_value = sign_test_p(on_only, off_only) if pairs else None
        verdict: EnumVerdict | None = None
        if not underpowered and p_value is not None:
            if p_value < _ALPHA and on_only > off_only:
                verdict = EnumVerdict.CONTEXT_HELPS
            elif p_value < _ALPHA and off_only > on_only:
                verdict = EnumVerdict.CONTEXT_HURTS
            else:
                verdict = EnumVerdict.NO_DETECTABLE_DIFFERENCE

        for arm in EnumBenchmarkArm:
            arm_runs = list(by_arm[arm].values())
            checked = [r for r in arm_runs if r.outcome in _CHECKED]
            errors = [r for r in arm_runs if r.outcome is EnumRunOutcome.ERROR]
            attempted = checked + errors
            rows.append(
                ModelReportRow(
                    model_id=model_id,
                    lane=lane,
                    condition=arm,
                    tasks_run=len(attempted),
                    passes=sum(r.outcome is EnumRunOutcome.PASS for r in checked),
                    not_applicable=sum(
                        r.outcome is EnumRunOutcome.NOT_APPLICABLE for r in arm_runs
                    ),
                    blocked=sum(r.outcome is EnumRunOutcome.BLOCKED for r in arm_runs),
                    errors=len(errors),
                    mean_tokens=_mean(
                        [t for r in checked if (t := total_tokens(r)) is not None]
                    ),
                    mean_fresh_tokens=_mean(
                        [t for r in checked if (t := fresh_tokens(r)) is not None]
                    ),
                    paired_tasks=len(pairs),
                    paired_pass_rate_diff=rate_diff,
                    paired_mean_tokens_diff=token_diff,
                    on_only_passes=on_only,
                    off_only_passes=off_only,
                    sign_test_p=p_value,
                    underpowered=underpowered,
                    verdict=verdict,
                )
            )
    return tuple(rows)


def _fmt(value: float | str | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    return f"{value:.1f}"


def render_markdown(rows: tuple[ModelReportRow, ...], *, min_paired_tasks: int) -> str:
    head = (
        "| model | lane | condition | tasks | passes | n/a | blocked | errors "
        "| mean tokens | mean fresh tokens | paired tasks | on-off pass diff "
        "| on-off mean tokens diff | underpowered | verdict |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {r.model_id} | {r.lane.value} | {r.condition.value} | {r.tasks_run} "
        f"| {r.passes} | {r.not_applicable} | {r.blocked} | {r.errors} "
        f"| {_fmt(r.mean_tokens)} | {_fmt(r.mean_fresh_tokens)} | {r.paired_tasks} "
        f"| {_fmt(r.paired_pass_rate_diff) if r.paired_pass_rate_diff is None else f'{r.paired_pass_rate_diff:+.2f}'} "
        f"| {_fmt(r.paired_mean_tokens_diff)} "
        f"| {'yes (no verdict)' if r.underpowered else 'no'} "
        f"| {r.verdict.value if r.verdict else '-'} |\n"
        for r in rows
    )
    return (
        head
        + body
        + f"\nUnderpowered = fewer than {min_paired_tasks} paired tasks (a pass or fail "
        "in both arms). Tokens: mean of input + output + cache read + cache write over "
        "runs where all four are known; UNKNOWN when none are. Verdict: exact two-sided "
        "sign test over discordant pairs at 0.05, only when not underpowered.\n"
    )
