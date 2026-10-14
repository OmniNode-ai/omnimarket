# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge ladder result files into the tables the report quotes (OMN-18300).

A run of the full ladder is not always one file. A rung whose fixture changed
has to be re-run, and a transport failure that made a delegation unreadable has
to be re-run as plumbing rather than reported as a model result. So results
arrive as several files and this tool merges them, later files winning per task.

Two things it will not do, because both would turn a merge into a selection:

* It never picks the better of two records for the same task. Precedence is the
  order the files are given on the command line, which the report states.
* It never drops a refusal. A refused task stays refused in the table, counted
  against its rung, with its reason carried through.

The tables it prints are the tables the report quotes. Nothing in the report is
typed by hand from a screen.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

RUNG_ORDER = ["R1", "R2", "R3", "R3b", "R4", "R5", "R6"]

# The five task classes the delegation benchmark reports, in report order.
CLASS_ORDER = ["document", "review", "test", "reasoning", "code_generation"]

# The smallest class a separation claim may rest on (OMN-20844 AC2).
MINIMUM_TASKS_PER_CLASS = 25


def merge(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Later files win per task id. Returns the records and the run metadata."""
    records: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    for path in paths:
        blob = json.loads(path.read_text(encoding="utf-8"))
        meta = {k: v for k, v in blob.items() if k != "records"}
        for record in blob["records"]:
            records[record["task_id"]] = record
    return records, meta


def _rate(group: list[dict[str, Any]], predicate: Any) -> float:
    return (sum(1 for r in group if predicate(r)) / len(group)) if group else 0.0


def rung_table(
    records: dict[str, dict[str, Any]], tier: str | None = None
) -> list[dict[str, Any]]:
    """Per-rung figures. ``tier`` restricts to tasks a named tier answered.

    The restriction exists because disabling PAID escalation does not confine a
    delegation to the local model: a free frontier tier is still tried when the
    quality gate refuses the local candidate, and the text that comes back is
    then not the local model's. A table that does not separate those is a table
    about the escalation chain wearing the local model's name.
    """
    by_rung: dict[str, list[dict[str, Any]]] = {}
    for record in records.values():
        if tier is not None and record.get("tier") != tier:
            continue
        by_rung.setdefault(record["rung"], []).append(record)

    rows: list[dict[str, Any]] = []
    for rung in RUNG_ORDER:
        group = by_rung.get(rung, [])
        if not group:
            continue
        answered = [r for r in group if r["score"]["outcome"] != "refused"]
        latencies = [
            r["model_latency_ms"] for r in group if r["model_latency_ms"] is not None
        ]
        costs = [r["cost_usd"] for r in group if r["cost_usd"] is not None]
        rows.append(
            {
                "rung": rung,
                "tasks": len(group),
                "passed": sum(1 for r in group if r["score"]["outcome"] == "pass"),
                "failed": sum(1 for r in group if r["score"]["outcome"] == "fail"),
                "refused": sum(1 for r in group if r["score"]["outcome"] == "refused"),
                "pass_rate": _rate(group, lambda r: r["score"]["outcome"] == "pass"),
                "pass_rate_of_answered": (
                    _rate(answered, lambda r: r["score"]["outcome"] == "pass")
                    if answered
                    else None
                ),
                "leak_rate": _rate(group, lambda r: r["leak_fraction"] > 0.0),
                "mean_leak_fraction": (
                    sum(r["leak_fraction"] for r in group) / len(group)
                ),
                "median_model_latency_ms": (
                    sorted(latencies)[len(latencies) // 2] if latencies else None
                ),
                "median_wall_s": sorted(r["wall_ms"] for r in group)[len(group) // 2]
                / 1000,
                "total_cost_usd": sum(costs) if costs else None,
                "derived_rung_agrees": sum(
                    1 for r in group if r.get("derived_rung") == rung
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------
# Per-class, per-rung table (OMN-20844)
# --------------------------------------------------------------------------


def wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Two-sided Wilson score interval for a share. ``(0, 1)`` when ``n`` is 0.

    Wilson rather than the normal approximation: at shares near 0 or 1 and at
    n of a few dozen the normal interval leaves [0, 1], and an interval that
    does is not an interval anyone should compare rungs by.
    """
    if n <= 0:
        return 0.0, 1.0
    p = passes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def class_rung_table(
    records: Sequence[dict[str, Any]],
    rung_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """One row per (task class, delegation rung) over rung-run records.

    Each record is one task answered by one ``model_rung``. A task that came
    back with no answer is a row member that did not pass: dropping it would
    raise a rung's share by the very failures that distinguish it.

    ``accepted`` is the share the benchmark quotes: delivered past the
    delegation quality gate AND passed by the class scorer.
    ``answer_correct`` is the scorer's verdict alone, so a correct answer the
    gate refused is visible as ``gate_refused_correct`` and is neither counted
    as accepted nor mistaken for a wrong answer.
    """
    classes = [c for c in CLASS_ORDER if any(r["task_type"] == c for r in records)]
    classes += sorted({r["task_type"] for r in records} - set(CLASS_ORDER))
    seen: list[str] = []
    for record in records:
        if record["model_rung"] not in seen:
            seen.append(record["model_rung"])
    order = list(rung_order) if rung_order else seen
    order += [name for name in seen if name not in order]

    rows: list[dict[str, Any]] = []
    for task_type in classes:
        for model_rung in order:
            group = [
                r
                for r in records
                if r["task_type"] == task_type and r["model_rung"] == model_rung
            ]
            if not group:
                continue
            n = len(group)
            accepted = sum(1 for r in group if r["accepted"])
            correct = sum(1 for r in group if r["scorer_outcome"] == "pass")
            low, high = wilson_interval(accepted, n)
            latencies = [
                r["model_latency_ms"] / 1000
                for r in group
                if r.get("model_latency_ms") is not None
            ]
            costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
            rows.append(
                {
                    "task_type": task_type,
                    "model_rung": model_rung,
                    "tasks": n,
                    "accepted": accepted,
                    "accepted_share": accepted / n,
                    "interval_low": low,
                    "interval_high": high,
                    "answer_correct": correct,
                    "gate_refused_correct": sum(
                        1
                        for r in group
                        if r["scorer_outcome"] == "pass" and not r["accepted"]
                    ),
                    "no_answer": sum(
                        1 for r in group if r["scorer_outcome"] == "no_answer"
                    ),
                    "median_wall_s": _median([r["wall_ms"] / 1000 for r in group]),
                    "median_model_latency_s": _median(latencies),
                    "model_latency_receipts": len(latencies),
                    "cost_usd": sum(costs) if costs else None,
                    "cost_receipts": len(costs),
                    "meets_minimum_tasks": n >= MINIMUM_TASKS_PER_CLASS,
                }
            )
    return rows


def class_separation(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Which rungs, per class, have an interval that clears every other rung's.

    Two rungs are separated when their 95% intervals do not overlap. A rung is
    named ``separated`` only when it is disjoint from *every* other rung in the
    class: being clear of one rung and tangled with another is not a ranking.
    ``disjoint_pairs`` lists each disjoint pair as ``(higher, lower)``.
    """
    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_class.setdefault(row["task_type"], []).append(row)

    result: dict[str, dict[str, Any]] = {}
    for task_type, group in by_class.items():
        pairs: list[tuple[str, str]] = []
        for a in group:
            for b in group:
                if a is b:
                    continue
                if a["interval_low"] > b["interval_high"]:
                    pairs.append((a["model_rung"], b["model_rung"]))
        separated: list[str] = []
        if len(group) > 1:
            for row in group:
                name = row["model_rung"]
                others = [r["model_rung"] for r in group if r is not row]
                if all((name, o) in pairs or (o, name) in pairs for o in others):
                    separated.append(name)
        result[task_type] = {
            "separated": separated,
            "disjoint_pairs": pairs,
            "tasks": min(r["tasks"] for r in group),
        }
    return result


def render_table(
    rows: Sequence[dict[str, Any]], separation: dict[str, dict[str, Any]]
) -> str:
    """Markdown the report quotes verbatim: nothing in it is typed by hand."""
    lines: list[str] = []
    current: str | None = None
    for row in rows:
        if row["task_type"] != current:
            current = row["task_type"]
            info = separation.get(current, {})
            verdict = (
                "separated: " + ", ".join(info["separated"])
                if info.get("separated")
                else "separated: none (every rung's interval overlaps another's)"
            )
            lines += [
                "",
                f"### {current} ({info.get('tasks', row['tasks'])} tasks) - {verdict}",
                "",
                "| rung | accepted | share | 95% interval | answer correct "
                "| no answer | median wall s | median model s | cost USD |",
                "| -- | -- | -- | -- | -- | -- | -- | -- | -- |",
            ]
        latency = row["median_model_latency_s"]
        cost = row["cost_usd"]
        lines.append(
            f"| {row['model_rung']} | {row['accepted']}/{row['tasks']} "
            f"| {row['accepted_share']:.0%} "
            f"| {row['interval_low']:.0%}-{row['interval_high']:.0%} "
            f"| {row['answer_correct']} | {row['no_answer']} "
            f"| {row['median_wall_s']:.1f} "
            f"| {'n/a' if latency is None else f'{latency:.1f}'} "
            f"| {'not measured' if cost is None else f'{cost:.4f} ({row["cost_receipts"]} receipts)'} |"
        )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--tier",
        default=None,
        help="Restrict the table to tasks answered at this tier, so an escalated "
        "answer is not counted as the tier below it.",
    )
    args = parser.parse_args()

    records, meta = merge(args.results)
    rows = rung_table(records, args.tier)
    if args.tier:
        print(f"restricted to tier={args.tier}")

    print(f"merged {len(args.results)} file(s) -> {len(records)} task records")
    print(f"precedence: {' < '.join(p.name for p in args.results)}\n")
    header = (
        f"{'rung':<6}{'n':>3}{'pass':>6}{'fail':>6}{'refus':>6}{'rate':>7}"
        f"{'leak':>7}{'lat_ms':>9}{'wall_s':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        latency = row["median_model_latency_ms"]
        print(
            f"{row['rung']:<6}{row['tasks']:>3}{row['passed']:>6}{row['failed']:>6}"
            f"{row['refused']:>6}{row['pass_rate']:>7.2f}{row['leak_rate']:>7.2f}"
            f"{(latency if latency is not None else -1):>9}{row['median_wall_s']:>8.1f}"
        )

    tiers: dict[str, int] = {}
    for record in records.values():
        tiers[str(record.get("tier"))] = tiers.get(str(record.get("tier")), 0) + 1
    print(f"\nanswered by tier: {tiers}")

    print("\nper task:")
    for task_id in sorted(records, key=lambda t: (t.split("-")[0], t)):
        record = records[task_id]
        derived = record.get("derived_rung")
        mark = "" if derived == record["rung"] else f"  derived={derived}"
        print(
            f"  {task_id:<8}{record['score']['outcome']:<9}"
            f"score={record['score']['score']:.2f} leak={record['leak_fraction']:.2f} "
            f"tier={record['tier']} model={record['model_id']}{mark}"
        )

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "meta": meta,
                    "sources": [str(p) for p in args.results],
                    "rung_table": rows,
                    "records": [records[k] for k in sorted(records)],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
