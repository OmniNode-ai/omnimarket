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
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))


RUNG_ORDER = ["R1", "R2", "R3", "R3b", "R4", "R5", "R6"]


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


DIFFICULTY_ORDER = ["easy", "medium", "hard"]


def wilson_interval(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a binomial share, including both endpoints."""
    if n == 0:
        return 0.0, 1.0
    share = k / n
    denominator = 1 + z * z / n
    centre = (share + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / denominator
    return (
        0.0 if k == 0 else max(0.0, centre - radius),
        1.0 if k == n else min(1.0, centre + radius),
    )


def load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    """Later files win per backend and task, irrespective of their verdict."""
    records: dict[tuple[str | None, str], dict[str, Any]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records[(record.get("backend"), record["task_id"])] = record
    return list(records.values())


def is_unreached(record: dict[str, Any]) -> bool:
    """True when the delegate never produced a model answer or a gate verdict.

    A model that answered wrongly has a response or an attempt with a verdict;
    a request refused before any rung ran, or a client that timed out, has
    neither. Harness (GLM) cells carry no attempt list, so they are judged by
    their own recorded outcome instead. A call that ran out of its time budget
    is a failure to deliver, not an unreached cell.
    """
    if record["score"]["outcome"] == "harness_error":
        return True
    notes = str(record.get("notes", ""))
    if record["score"]["outcome"] == "refused":
        # Running out of the call budget is the rung failing to deliver, so it
        # stays in n; a refusal before any attempt is the route's own.
        return not ("client exceeded" in notes or "timed_out=True" in notes)
    if notes.startswith("status=") and not record.get("attempts"):
        return bool(record.get("response_chars", 0) == 0)
    return False


def _accepted(record: dict[str, Any]) -> bool:
    from benchmarks.delegation_ladder.models import EnumOutcome, is_accepted

    accepted = record.get("accepted")
    if accepted is not None:
        return bool(accepted)
    return is_accepted(
        EnumOutcome(record["score"]["outcome"]), record.get("quality_gate_passed")
    )


def _class_metrics(group: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(group)
    accepted = sum(_accepted(r) for r in group)
    lo, hi = wilson_interval(accepted, n)
    latencies = [
        r["model_latency_ms"] for r in group if r.get("model_latency_ms") is not None
    ]
    costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
    return {
        "n": n,
        "accepted": accepted,
        "share": accepted / n,
        "lo": lo,
        "hi": hi,
        "scorer_pass": sum(r["score"]["outcome"] == "pass" for r in group),
        "gate_refused": sum(
            r["score"]["outcome"] == "pass" and not _accepted(r) for r in group
        ),
        "failed": sum(r["score"]["outcome"] == "fail" for r in group),
        "refused": sum(r["score"]["outcome"] == "refused" for r in group),
        # The node's gate vetoed the answer and returned none, so the scorer saw
        # nothing: not accepted, but not evidence the model was wrong either.
        "gate_no_answer": sum(
            r.get("quality_gate_passed") is False and r.get("response_chars", 0) == 0
            for r in group
        ),
        "median_model_latency_ms": statistics.median(latencies) if latencies else None,
        "median_wall_s": statistics.median(r["wall_ms"] for r in group) / 1000,
        "cost_usd": sum(costs) if costs else None,
        "cost_receipts": len(costs),
    }


def class_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Acceptance and scorer-only counts per task class and delegation rung."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (str(record.get("task_type")), str(record.get("backend")))
        groups.setdefault(key, []).append(record)
    return [
        {
            "task_type": task_type,
            "backend": backend,
            **_class_metrics(groups[(task_type, backend)]),
        }
        for task_type, backend in sorted(groups)
    ]


def tier_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            str(record.get("task_type")),
            str(record.get("difficulty")),
            str(record.get("backend")),
        )
        groups.setdefault(key, []).append(record)
    keys = sorted(
        groups,
        key=lambda key: (
            key[0],
            DIFFICULTY_ORDER.index(key[1]) if key[1] in DIFFICULTY_ORDER else 3,
            key[2],
        ),
    )
    return [
        {
            "task_type": task_type,
            "difficulty": difficulty,
            "backend": backend,
            **_class_metrics(groups[(task_type, difficulty, backend)]),
        }
        for task_type, difficulty, backend in keys
    ]


def first_failing_tier(
    rows: list[dict[str, Any]],
    fail_below: float = 0.8,
) -> dict[tuple[str, str], str | None]:
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        groups.setdefault((row["task_type"], row["backend"]), {})[row["difficulty"]] = (
            row["share"]
        )
    return {
        key: next(
            (
                tier
                for tier in DIFFICULTY_ORDER
                if tier in shares and shares[tier] < fail_below
            ),
            None,
        )
        for key, shares in sorted(groups.items())
    }


def separation(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A backend is separated only if its Wilson interval is disjoint from
    EVERY other backend's interval in that class. Pairwise disjointness alone
    does not separate a backend that overlaps a third one.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["task_type"], []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for task_type, group in sorted(groups.items()):
        group = sorted(group, key=lambda r: r["backend"])
        disjoint: list[tuple[str, str]] = []
        overlapping: list[tuple[str, str]] = []
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                target = (
                    disjoint if a["hi"] < b["lo"] or b["hi"] < a["lo"] else overlapping
                )
                target.append((a["backend"], b["backend"]))
        # One rung has nothing to be separated from; "disjoint from every other"
        # would otherwise be vacuously true.
        separated = (
            [
                r["backend"]
                for r in group
                if all(r["backend"] not in pair for pair in overlapping)
            ]
            if len(group) > 1
            else []
        )
        result[task_type] = {
            "separated": separated,
            "overlapping_pairs": overlapping,
            "disjoint_pairs": disjoint,
            "verdict": "separated: " + ", ".join(separated)
            if separated
            else "none separated",
        }
    return result


def escalation_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("backend") != "unpinned":
            continue
        attempts = record.get("attempts") or []
        answering: dict[str, Any] = next(
            (a for a in reversed(attempts) if a.get("acceptance_decision") == "accept"),
            {},
        )
        rows.append(
            {
                "task_id": record["task_id"],
                "answering_backend": answering.get("backend_id") or "none",
                "escalation_count": record.get("escalation_count"),
                "refused_attempts": [
                    f"{a.get('backend_id')}:{a.get('acceptance_decision')}:{a.get('acceptance_reason')}"
                    for a in attempts
                    if a.get("acceptance_decision") != "accept"
                ],
                "wall_ms": record["wall_ms"],
                "scorer": record["score"]["outcome"],
                "accepted": record.get("accepted"),
            }
        )
    return sorted(rows, key=lambda row: row["task_id"])


def _markdown_table(headers: list[str], cells: list[list[str]]) -> None:
    def escaped(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in cells:
        print("| " + " | ".join(escaped(value) for value in row) + " |")
    print()


def _print_class_table(rows: list[dict[str, Any]], *, tiered: bool = False) -> None:
    headers = (
        ["class"]
        + (["difficulty"] if tiered else [])
        + [
            "backend",
            "n",
            "accepted",
            "share",
            "95% CI",
            "median model latency ms",
            "median wall s",
            "cost",
            "gate-refused, no answer",
        ]
    )
    cells: list[list[str]] = []
    for row in rows:
        cost = (
            f"${row['cost_usd']:.4f} ({row['cost_receipts']}/{row['n']} receipts)"
            if row["cost_usd"] is not None
            else "no cost receipt"
        )
        latency = row["median_model_latency_ms"]
        cells.append(
            [row["task_type"]]
            + ([row["difficulty"]] if tiered else [])
            + [
                row["backend"],
                str(row["n"]),
                str(row["accepted"]),
                f"{row['share']:.1%}",
                f"{row['lo']:.1%} - {row['hi']:.1%}",
                str(latency) if latency is not None else "—",
                f"{row['median_wall_s']:.1f}",
                cost,
                str(row["gate_no_answer"]),
            ]
        )
    _markdown_table(headers, cells)


def _jsonl_report(
    paths: list[Path], out: Path | None, aliases: dict[str, str] | None = None
) -> int:
    records = load_jsonl(paths)
    for record in records:
        # One model served from two hosts is one rung: --alias folds the second
        # backend id into the first and the host column keeps the difference.
        record["backend"] = (aliases or {}).get(record["backend"], record["backend"])
    # The unpinned ladder is the chain, not a rung; it has its own table below.
    on_rungs = [r for r in records if r.get("backend") != "unpinned"]
    # A request that never reached a model (no attempt, no answer, a refusal in
    # the delegate's own plumbing) measures the route, not the rung. Counting it
    # as a wrong answer would hand a rung a capability regression it does not
    # have, so those cells are listed apart and left out of n.
    unreached = [r for r in on_rungs if is_unreached(r)]
    pinned = [r for r in on_rungs if not is_unreached(r)]
    if unreached:
        print(
            f"Unreached (excluded from n): {len(unreached)} cells: "
            + ", ".join(
                f"{b}={sum(1 for r in unreached if r['backend'] == b)}"
                for b in sorted({r["backend"] for r in unreached})
            )
            + "\n"
        )
    classes, tiers = class_rows(pinned), tier_rows(pinned)
    failing = first_failing_tier(tiers)
    separated = separation(classes)
    escalations = escalation_rows(records)
    _print_class_table(classes)
    _print_class_table(tiers, tiered=True)
    for (task_type, backend), difficulty in failing.items():
        print(f"First failing tier: {task_type} / {backend}: {difficulty or 'none'}")
    print()
    for task_type, verdict in separated.items():
        print(f"{task_type}: {verdict['verdict']}")
        print(
            f"  pairwise disjoint: {verdict['disjoint_pairs']}; overlapping: {verdict['overlapping_pairs']}"
        )
    print()
    _markdown_table(
        [
            "task",
            "answering backend",
            "escalations",
            "refused attempts",
            "total wall ms",
            "scorer",
            "accepted",
        ],
        [
            [
                r["task_id"],
                r["answering_backend"],
                str(r["escalation_count"]),
                "; ".join(r["refused_attempts"]),
                str(r["wall_ms"]),
                str(r["scorer"]),
                str(r["accepted"]),
            ]
            for r in escalations
        ],
    )
    if out:
        out.write_text(
            json.dumps(
                {
                    "sources": [str(p) for p in paths],
                    "class_rows": classes,
                    "tier_rows": tiers,
                    "first_failing_tier": [
                        {"task_type": key[0], "backend": key[1], "difficulty": value}
                        for key, value in failing.items()
                    ],
                    "separation": separated,
                    "escalation_rows": escalations,
                    "records": records,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="*", type=Path)
    parser.add_argument("--jsonl", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="FROM=TO",
        help="Report backend FROM as backend TO (the same model on another host).",
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Restrict the table to tasks answered at this tier, so an escalated "
        "answer is not counted as the tier below it.",
    )
    args = parser.parse_args()
    if bool(args.results) == bool(args.jsonl):
        parser.error("provide either positional merged-JSON results or --jsonl FILE...")
    if args.jsonl:
        return _jsonl_report(
            args.jsonl,
            args.out,
            dict(a.split("=", 1) for a in args.alias),
        )

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
