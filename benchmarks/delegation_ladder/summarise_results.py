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
            # A pinned row the ladder moved to another backend is that backend's
            # answer under this rung's name: it is never accepted for this rung.
            moved = sum(1 for r in group if r.get("backend_pin_honoured") is False)
            accepted = sum(
                1
                for r in group
                if r["accepted"] and r.get("backend_pin_honoured") is not False
            )
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
                        if r["scorer_outcome"] == "pass"
                        and not (
                            r["accepted"] and r.get("backend_pin_honoured") is not False
                        )
                    ),
                    "pin_moved": moved,
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


# --------------------------------------------------------------------------
# Difficulty tiers (OMN-20844)
# --------------------------------------------------------------------------

DIFFICULTY_ORDER = ["easy", "medium", "hard"]

# Where a class's tiers begin, in characters of the prompt a rung is fed:
# (first medium prompt, first hard prompt). Fixed constants, not percentiles of
# whatever set is loaded, so adding a task never moves another task's tier. They
# were set on the committed tasks so each tier holds at least eight of them.
# The only spread the committed tasks have is the size of their input (every
# test task declares 3 dependent steps and every code_generation task 4), so a
# tier here ranks input size; it does not claim to rank intrinsic difficulty.
DIFFICULTY_THRESHOLDS: dict[str, tuple[int, int]] = {
    "test": (3_000, 5_000),
    "code_generation": (7_000, 9_000),
}


def difficulty_tier(task_type: str, prompt_chars: int) -> str | None:
    """The tier of a task, or None for a class with no declared thresholds."""
    thresholds = DIFFICULTY_THRESHOLDS.get(task_type)
    if thresholds is None:
        return None
    medium_from, hard_from = thresholds
    if prompt_chars >= hard_from:
        return "hard"
    return "medium" if prompt_chars >= medium_from else "easy"


def tier_rung_table(
    records: Sequence[dict[str, Any]],
    tiers: dict[str, str],
    rung_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """The class table split by difficulty tier.

    ``tiers`` maps a task id to its tier. A record whose task has no tier is
    left out: filing it under a default tier would put its answer in a share it
    does not belong to. Rows are the same rows ``class_rung_table`` builds, one
    per (class, tier, rung), so a tier's share, interval, latency and cost are
    figured the same way as a class's.
    """
    tiered = [
        {**r, "difficulty": tiers[r["task_id"]]}
        for r in records
        if r["task_id"] in tiers
    ]
    rows: list[dict[str, Any]] = []
    for difficulty in DIFFICULTY_ORDER:
        group = [r for r in tiered if r["difficulty"] == difficulty]
        for row in class_rung_table(group, rung_order):
            rows.append({**row, "difficulty": difficulty})
    classes = [c for c in CLASS_ORDER if any(r["task_type"] == c for r in tiered)]
    classes += sorted({r["task_type"] for r in tiered} - set(CLASS_ORDER))
    order = {c: i for i, c in enumerate(classes)}
    rows.sort(
        key=lambda r: (order[r["task_type"]], DIFFICULTY_ORDER.index(r["difficulty"]))
    )
    return rows


def first_failing_tier(
    rows: Sequence[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, str | None]]:
    """Per (class, rung): the first tier with any miss, and the first with a majority of misses.

    The majority line is the one a routing argument can lean on. With eight
    tasks a tier the rung only just misses is inside its interval's noise, so
    both are reported and neither is called a threshold of the rung.
    """
    found: dict[tuple[str, str], dict[str, str | None]] = {}
    for row in rows:
        key = (row["task_type"], row["model_rung"])
        entry = found.setdefault(key, {"first_miss": None, "first_majority_fail": None})
        if entry["first_miss"] is None and row["accepted"] < row["tasks"]:
            entry["first_miss"] = row["difficulty"]
        if entry["first_majority_fail"] is None and row["accepted_share"] < 0.5:
            entry["first_majority_fail"] = row["difficulty"]
    return found


def render_difficulty_table(
    rows: Sequence[dict[str, Any]],
    failing: dict[tuple[str, str], dict[str, str | None]],
) -> str:
    """Markdown for the report: per class, a row per (rung, tier), then where each rung fails."""
    lines: list[str] = []
    current: str | None = None
    for row in rows:
        if row["task_type"] != current:
            current = row["task_type"]
            lines += [
                "",
                f"### {current} by difficulty",
                "",
                "| rung | tier | accepted | share | 95% interval | answer correct "
                "| no answer | median wall s | median model s |",
                "| -- | -- | -- | -- | -- | -- | -- | -- | -- |",
            ]
        latency = row["median_model_latency_s"]
        lines.append(
            f"| {row['model_rung']} | {row['difficulty']} "
            f"| {row['accepted']}/{row['tasks']} | {row['accepted_share']:.0%} "
            f"| {row['interval_low']:.0%}-{row['interval_high']:.0%} "
            f"| {row['answer_correct']} | {row['no_answer']} "
            f"| {row['median_wall_s']:.1f} "
            f"| {'n/a' if latency is None else f'{latency:.1f}'} |"
        )
    lines += ["", "### where each rung starts failing", ""]
    for (task_type, model_rung), entry in sorted(failing.items()):
        lines.append(
            f"- {task_type} / {model_rung}: first miss: {entry['first_miss'] or 'none'}; "
            f"first majority failure: {entry['first_majority_fail'] or 'none'}"
        )
    return "\n".join(lines).strip() + "\n"


def render_ladder_table(records: Sequence[dict[str, Any]]) -> str:
    """Markdown for the unpinned-ladder run: one row per task, then who answered.

    Each record carries the ``ladder`` trace of its own run. ``climbed`` is a
    run that tried more than one rung; a rung that was refused is listed with the
    gate rule or verdict the receipt gave for it.
    """
    lines = [
        "| task | class | answered by | escalations | accepted | wall s | refused rungs (why) |",
        "| -- | -- | -- | -- | -- | -- | -- |",
    ]
    answered: dict[str, int] = {}
    climbed = 0
    gate_took_wrong = 0
    for record in sorted(records, key=lambda r: r["task_id"]):
        # A run with no receipt has no trace: it answered nobody and tried one rung.
        trace = record.get("ladder") or {
            "answered_by": None,
            "escalations": 0,
            "refused": [],
        }
        who = trace["answered_by"]
        answered[who or "no rung"] = answered.get(who or "no rung", 0) + 1
        climbed += 1 if trace["escalations"] > 0 else 0
        gate_took_wrong += 1 if who and not record["accepted"] else 0
        why = "; ".join(
            f"{r['backend_id']}: {r['reason'] or r['decision']}"
            for r in trace["refused"]
        )
        lines.append(
            f"| {record['task_id']} | {record['task_type']} | {who or 'none'} "
            f"| {trace['escalations']} | {'yes' if record['accepted'] else 'no'} "
            f"| {record['wall_ms'] / 1000:.1f} | {why or '-'} |"
        )
    total = len(records)
    lines += ["", f"tasks: {total}"]
    for name, count in sorted(answered.items(), key=lambda kv: (-kv[1], kv[0])):
        label = "no rung" if name == "no rung" else name
        lines.append(f"- {label} answered {count} of {total}")
    lines.append(f"climbed at least once: {climbed} of {total}")
    lines.append(f"gate accepted, scorer failed: {gate_took_wrong} of {total}")
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


# --------------------------------------------------------------------------
# Structural features of the hard corpus (OMN-20844)
# --------------------------------------------------------------------------

# Prompt characters, measured. The receipts read about 4.1 characters per
# token on these prompts, so "over 150k" is upward of 37k tokens, beyond a
# 32768-token slot and toward the 65536 tokens the local-coder rung declares.
CONTEXT_BINS: tuple[tuple[str, int], ...] = (
    ("under 60k chars", 60_000),
    ("60k-150k chars", 150_000),
    ("over 150k chars", 10**9),
)

FEATURE_SPLITS: tuple[tuple[str, str, str], ...] = (
    ("files touched", "1 file", "2-3 files"),
    ("cross-file cause", "no", "yes"),
)


def bundle_features(scorer_config: dict[str, object]) -> dict[str, Any] | None:
    """A hard task's recorded structural features, or None for any other task."""
    features = scorer_config.get("features")
    return dict(features) if isinstance(features, dict) else None


def context_bin(chars: int) -> str:
    return next(label for label, bound in CONTEXT_BINS if chars < bound)


def feature_levels(features: dict[str, Any]) -> dict[str, str]:
    """One level per structural feature, from a bundle's recorded features."""
    return {
        "files touched": "1 file"
        if int(features["files_touched"]) == 1
        else "2-3 files",
        "cross-file cause": "yes" if features["cross_file_cause"] else "no",
        "context size": context_bin(int(features["context_chars"])),
    }


def newcombe_difference(
    k1: int, n1: int, k2: int, n2: int, z: float = 1.96
) -> tuple[float, float, float]:
    """``p2 - p1`` and its Newcombe (Wilson hybrid) 95% interval."""
    p1 = k1 / n1 if n1 else 0.0
    p2 = k2 / n2 if n2 else 0.0
    l1, u1 = wilson_interval(k1, n1, z)
    l2, u2 = wilson_interval(k2, n2, z)
    d = p2 - p1
    low = d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    high = d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return d, low, high


def _share(k: int, n: int) -> str:
    if n == 0:
        return "0/0"
    low, high = wilson_interval(k, n)
    return f"{k}/{n} {k / n:.0%} ({low:.0%}-{high:.0%})"


def _correct(record: dict[str, Any]) -> bool:
    return record.get("scorer_outcome") == "pass"


def _answered(record: dict[str, Any]) -> bool:
    return record.get("scorer_outcome") != "no_answer"


def _left_local(record: dict[str, Any]) -> bool:
    trace = record.get("ladder") or {}
    tiers = [trace.get("answered_tier")] + [
        attempt.get("tier") for attempt in trace.get("refused") or []
    ]
    return any(tier not in (None, "local") for tier in tiers)


def feature_rows(
    records: Sequence[dict[str, Any]],
    features: dict[str, dict[str, Any]],
    pinned: Sequence[str],
    ladder: str = "unpinned-ladder",
) -> list[dict[str, Any]]:
    """Per feature level: failed pinned cells, failed tasks, ladder escalations.

    A cell fails when the mechanical scorer did not pass its answer (no answer
    counts as a failure); "not accepted" also counts a correct answer that the
    delegation's own quality gate refused, and "failed, answer seen" counts
    only cells whose answer reached the scorer (a gate that withholds the
    answer, or a harness out of time, leaves nothing to score). A task fails
    when fewer than half
    of the pinned rungs that ran it produced a passing answer. The ladder
    escalated when its receipt tried more than one rung, and left the local
    tier when any rung it tried was not local (routing can send a large input
    straight to a cloud rung, which tries one rung and escalates nothing).
    """
    levels = {task: feature_levels(f) for task, f in features.items()}
    by_task: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec["task_id"] in levels and rec["model_rung"] in pinned:
            by_task.setdefault(rec["task_id"], []).append(rec)
    ladder_recs = {
        r["task_id"]: r
        for r in records
        if r["model_rung"] == ladder and r["task_id"] in levels
    }
    rows: list[dict[str, Any]] = []
    for feature in ("files touched", "cross-file cause", "context size"):
        order = [label for label, _ in CONTEXT_BINS]
        present = {lv[feature] for lv in levels.values()}
        ranked = (
            [label for label in order if label in present]
            if feature == "context size"
            else sorted(present)
        )
        for level in ranked:
            tasks = [t for t, lv in levels.items() if lv[feature] == level]
            cells = [r for t in tasks for r in by_task.get(t, [])]
            failed_tasks = [
                t
                for t in tasks
                if by_task.get(t)
                and sum(1 for r in by_task[t] if _correct(r)) * 2 < len(by_task[t])
            ]
            ran = [t for t in tasks if by_task.get(t)]
            lad = [ladder_recs[t] for t in tasks if t in ladder_recs]
            rows.append(
                {
                    "feature": feature,
                    "level": level,
                    "tasks": len(tasks),
                    "cells": len(cells),
                    "cells_failed": sum(1 for r in cells if not _correct(r)),
                    "cells_not_accepted": sum(1 for r in cells if not r["accepted"]),
                    "cells_answered": sum(1 for r in cells if _answered(r)),
                    "cells_answered_failed": sum(
                        1 for r in cells if _answered(r) and not _correct(r)
                    ),
                    "tasks_ran": len(ran),
                    "tasks_failed": len(failed_tasks),
                    "ladder_runs": len(lad),
                    "ladder_escalated": sum(
                        1
                        for r in lad
                        if (r.get("ladder") or {}).get("escalations", 0) > 0
                    ),
                    "ladder_left_local": sum(1 for r in lad if _left_local(r)),
                    "ladder_failed": sum(1 for r in lad if not _correct(r)),
                }
            )
    return rows


def _difference(
    first: dict[str, Any], last: dict[str, Any], key: str, n_key: str
) -> str:
    d, low, high = newcombe_difference(first[key], first[n_key], last[key], last[n_key])
    return f"{d:+.0%} ({low:+.0%} to {high:+.0%})"


def render_feature_table(rows: Sequence[dict[str, Any]]) -> str:
    out = [
        "| feature | level | tasks | pinned cells failed | failed, answer seen "
        "| pinned cells not accepted "
        "| tasks failed (most rungs) | ladder escalated | ladder left local "
        "| ladder failed |",
        "| -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |",
    ]
    for r in rows:
        out.append(
            f"| {r['feature']} | {r['level']} | {r['tasks']} "
            f"| {_share(r['cells_failed'], r['cells'])} "
            f"| {_share(r['cells_answered_failed'], r['cells_answered'])} "
            f"| {_share(r['cells_not_accepted'], r['cells'])} "
            f"| {_share(r['tasks_failed'], r['tasks_ran'])} "
            f"| {_share(r['ladder_escalated'], r['ladder_runs'])} "
            f"| {_share(r['ladder_left_local'], r['ladder_runs'])} "
            f"| {_share(r['ladder_failed'], r['ladder_runs'])} |"
        )
    out += [
        "",
        "Difference, last level minus first, with a Newcombe 95% interval:",
        "",
        "| feature | levels | pinned cells failed | failed, answer seen "
        "| tasks failed | ladder escalated "
        "| ladder left local |",
        "| -- | -- | -- | -- | -- | -- | -- |",
    ]
    for feature in ("files touched", "cross-file cause", "context size"):
        group = [r for r in rows if r["feature"] == feature]
        if len(group) < 2:
            continue
        first, last = group[0], group[-1]

        out.append(
            f"| {feature} | {last['level']} vs {first['level']} "
            f"| {_difference(first, last, 'cells_failed', 'cells')} "
            f"| {_difference(first, last, 'cells_answered_failed', 'cells_answered')} "
            f"| {_difference(first, last, 'tasks_failed', 'tasks_ran')} "
            f"| {_difference(first, last, 'ladder_escalated', 'ladder_runs')} "
            f"| {_difference(first, last, 'ladder_left_local', 'ladder_runs')} |"
        )
    return "\n".join(out)


def render_feature_report(
    records: Sequence[dict[str, Any]],
    features: dict[str, dict[str, Any]],
    task_types: dict[str, str],
    pinned: Sequence[str],
) -> str:
    """The feature table over every hard task, then over each class alone."""
    blocks = [
        f"### all hard tasks ({len(features)})\n\n"
        + render_feature_table(feature_rows(records, features, pinned))
    ]
    for task_type in sorted(set(task_types.values())):
        subset = {t: f for t, f in features.items() if task_types.get(t) == task_type}
        blocks.append(
            f"### {task_type} ({len(subset)})\n\n"
            + render_feature_table(feature_rows(records, subset, pinned))
        )
    return "\n\n".join(blocks)
