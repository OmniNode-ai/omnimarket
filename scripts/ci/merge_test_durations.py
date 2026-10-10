#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Balance the full-suite test shards by recorded duration (OMN-19684).

History. The first fix merged every shard's pytest-split ``--store-durations``
output into one ``actions/cache`` entry that the next run's ``--splits``/
``--group`` balancer read. Live PR runs after it still spread 1.75-2.10x
(runs 37932017787, 37933529792, 37934956505, 37940218989; job time, slowest
over fastest of 20). Two defects were left in that design:

* the balancer input was a mutable cache. A PR run only sees caches written on
  its own ref or the base branch, so shards could split by test count, and two
  shards could even read different maps;
* every shard collected the whole 40k-test tree (about 61 s on a lab host)
  before running its slice, so more than half of each shard's wall time was
  session overhead, not tests.

The balancer input is now a committed record, ``config/test_file_durations.json``
(test file -> seconds), identical on every runner. ``plan`` deals the test
files of the tree to the shards longest-first (LPT), so a shard runs exactly
its own files and collects only those. ``verify`` proves the plan is an exact
partition of the files pytest collects, so a file can never fall between
shards. ``record`` rebuilds the committed record from the per-shard
``--store-durations`` artifacts of full-suite CI runs. ``merge`` keeps the
per-test merge the nightly run's durations cache still reads.

Subcommands: merge, record, plan, verify.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import statistics
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_RECORD = Path("config/test_file_durations.json")
TESTS_DIR = "tests"

# pytest's default ``python_files`` and ``norecursedirs``. pyproject.toml sets
# neither, so these are the rules pytest applies to collect ``tests/``. A
# mismatch is caught by ``verify`` against a real ``--collect-only``.
PYTHON_FILE_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")
NORECURSE_PATTERNS: tuple[str, ...] = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)
FALLBACK_FILE_SECONDS = 1.0


def _read_duration_map(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} did not contain a JSON object of id -> duration")
    return {str(key): float(value) for key, value in data.items()}


def merge_durations(artifacts_dir: Path) -> dict[str, float]:
    shard_files = sorted(artifacts_dir.glob(".test_durations.*"))
    if not shard_files:
        raise SystemExit(
            f"no '.test_durations.<split>' files found under {artifacts_dir} "
            "-- the per-shard durations artifacts did not download"
        )

    merged: dict[str, float] = {}
    for shard_file in shard_files:
        shard_map = json.loads(shard_file.read_text(encoding="utf-8"))
        if not isinstance(shard_map, dict):
            raise SystemExit(
                f"{shard_file} did not contain a JSON object of test id -> duration"
            )
        for test_id, duration in shard_map.items():
            if test_id in merged and merged[test_id] != duration:
                raise SystemExit(
                    f"duration collision for {test_id!r}: {merged[test_id]} "
                    f"(earlier shard) != {duration} (from {shard_file.name}) -- "
                    "a test id should belong to exactly one shard"
                )
            merged[test_id] = duration
    return merged


def file_durations(test_durations: dict[str, float]) -> dict[str, float]:
    """Sum per-test seconds (``path::test`` ids) into per-file seconds."""
    totals: dict[str, float] = {}
    for test_id, seconds in test_durations.items():
        test_file = test_id.split("::", 1)[0]
        totals[test_file] = totals.get(test_file, 0.0) + seconds
    return totals


def mean_file_durations(runs: Sequence[dict[str, float]]) -> dict[str, float]:
    """Mean seconds per file over the runs that recorded it."""
    seen: dict[str, list[float]] = {}
    for run in runs:
        for test_file, seconds in run.items():
            seen.setdefault(test_file, []).append(seconds)
    return {name: sum(values) / len(values) for name, values in seen.items()}


def discover_test_files(root: Path, tests_dir: str = TESTS_DIR) -> list[str]:
    """Test files pytest would collect under ``root/tests_dir``, sorted."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root / tests_dir):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not any(fnmatch.fnmatchcase(name, pat) for pat in NORECURSE_PATTERNS)
        )
        for name in filenames:
            if any(fnmatch.fnmatchcase(name, pat) for pat in PYTHON_FILE_PATTERNS):
                relative = Path(dirpath, name).relative_to(root)
                found.append(relative.as_posix())
    return sorted(found)


def file_weights(
    files: Iterable[str], record: dict[str, float]
) -> tuple[dict[str, float], list[str]]:
    """Weight per file; an unrecorded file takes the record's median."""
    known = [seconds for seconds in record.values() if seconds > 0]
    fallback = statistics.median(known) if known else FALLBACK_FILE_SECONDS
    weights: dict[str, float] = {}
    unrecorded: list[str] = []
    for name in files:
        if name in record:
            weights[name] = record[name]
        else:
            weights[name] = fallback
            unrecorded.append(name)
    return weights, sorted(unrecorded)


def plan_shards(weights: dict[str, float], splits: int) -> list[list[str]]:
    """Deal files to ``splits`` shards longest-first onto the lightest shard.

    Deterministic: ties break on file name, then on shard index, so every
    runner that reads the same tree and the same record derives the same plan.
    """
    if splits < 1:
        raise SystemExit(f"splits must be at least 1, got {splits}")
    loads = [0.0] * splits
    shards: list[list[str]] = [[] for _ in range(splits)]
    for name in sorted(weights, key=lambda item: (-weights[item], item)):
        lightest = min(range(splits), key=lambda index: (loads[index], index))
        shards[lightest].append(name)
        loads[lightest] += weights[name]
    return [sorted(shard) for shard in shards]


def shard_loads(weights: dict[str, float], shards: list[list[str]]) -> list[float]:
    return [sum(weights[name] for name in shard) for shard in shards]


def collected_files(collect_output: str) -> set[str]:
    """Test files named by ``pytest --collect-only -q`` node ids."""
    names = (
        line.split("::", 1)[0].strip()
        for line in collect_output.splitlines()
        if "::" in line and not line.startswith((" ", "="))
    )
    return {name for name in names if name.endswith(".py") and " " not in name}


def verify_partition(shards: list[list[str]], expected: Iterable[str]) -> list[str]:
    """Problems that stop ``shards`` being an exact partition of ``expected``."""
    problems: list[str] = []
    assigned: dict[str, int] = {}
    for index, shard in enumerate(shards, start=1):
        for name in shard:
            if name in assigned:
                problems.append(
                    f"{name} is in shard {assigned[name]} and shard {index}"
                )
            assigned[name] = index
    for name in sorted(set(expected) - set(assigned)):
        problems.append(f"{name} is collected by pytest but in no shard")
    return problems


def _cmd_merge(args: argparse.Namespace) -> int:
    merged = merge_durations(args.artifacts_dir)
    args.output.write_text(json.dumps(merged), encoding="utf-8")
    print(
        f"merged {len(merged)} test durations from {args.artifacts_dir} into {args.output}"
    )
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    runs = [file_durations(merge_durations(path)) for path in args.artifacts_dir]
    record = {
        name: round(seconds, 2)
        for name, seconds in sorted(mean_file_durations(runs).items())
    }
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"recorded {len(record)} test files ({sum(record.values()):.1f} s) "
        f"from {len(runs)} run(s) into {args.output}"
    )
    return 0


def _plan(
    root: Path, record_path: Path, splits: int
) -> tuple[dict[str, float], list[str], list[list[str]]]:
    record = _read_duration_map(record_path)
    files = discover_test_files(root)
    if not files:
        raise SystemExit(f"no test files found under {root / TESTS_DIR}")
    weights, unrecorded = file_weights(files, record)
    return weights, unrecorded, plan_shards(weights, splits)


def _cmd_plan(args: argparse.Namespace) -> int:
    weights, unrecorded, shards = _plan(args.root, args.record, args.splits)
    if not 1 <= args.group <= args.splits:
        raise SystemExit(f"group {args.group} is outside 1..{args.splits}")
    mine = shards[args.group - 1]
    if not mine:
        raise SystemExit(
            f"shard {args.group}/{args.splits} has no test files; refusing to run "
            "pytest with no paths, which would run the whole default tree"
        )
    loads = shard_loads(weights, shards)
    sys.stderr.write(
        f"shard {args.group}/{args.splits}: {len(mine)} files, "
        f"{loads[args.group - 1]:.1f} s planned "
        f"(fleet {min(loads):.1f}-{max(loads):.1f} s, "
        f"{len(unrecorded)} of {len(weights)} files unrecorded)\n"
    )
    sys.stdout.write("\n".join(mine) + "\n")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    weights, unrecorded, shards = _plan(args.root, args.record, args.splits)
    expected = collected_files(args.collected.read_text(encoding="utf-8"))
    if not expected:
        raise SystemExit(
            f"{args.collected} names no test files -- the collect-only output is empty"
        )
    problems = verify_partition(shards, expected)
    loads = shard_loads(weights, shards)
    print(
        f"{len(expected)} collected test files, {len(weights)} planned across "
        f"{args.splits} shards ({min(loads):.1f}-{max(loads):.1f} s planned, "
        f"{len(unrecorded)} unrecorded)"
    )
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    merge = commands.add_parser("merge", help="union per-shard per-test durations")
    merge.add_argument("--artifacts-dir", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.set_defaults(handler=_cmd_merge)

    record = commands.add_parser(
        "record", help="build the committed per-file record from CI runs"
    )
    record.add_argument(
        "--artifacts-dir",
        type=Path,
        action="append",
        required=True,
        help="one downloaded run's '.test_durations.<split>' files; repeat per run",
    )
    record.add_argument("--output", type=Path, default=DEFAULT_RECORD)
    record.set_defaults(handler=_cmd_record)

    for name, handler, helptext in (
        ("plan", _cmd_plan, "print the test files of one shard"),
        ("verify", _cmd_verify, "prove the plan partitions what pytest collects"),
    ):
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("--splits", type=int, required=True)
        sub.add_argument("--record", type=Path, default=DEFAULT_RECORD)
        sub.add_argument("--root", type=Path, default=Path.cwd())
        if name == "plan":
            sub.add_argument("--group", type=int, required=True)
        else:
            sub.add_argument(
                "--collected",
                type=Path,
                required=True,
                help="output of `pytest tests/ --collect-only -q`",
            )
        sub.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
