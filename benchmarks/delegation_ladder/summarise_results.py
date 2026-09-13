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
