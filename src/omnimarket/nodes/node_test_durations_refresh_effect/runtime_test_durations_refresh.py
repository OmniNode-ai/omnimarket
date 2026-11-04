# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator and workflow entry point of the test-durations refresh (OMN-22389).

    uv run python -m omnimarket.nodes.node_test_durations_refresh_effect status \
        --files-out unrecorded.txt --github-output "$GITHUB_OUTPUT"
    uv run python -m omnimarket.nodes.node_test_durations_refresh_effect supplement \
        --durations refresh.test_durations --base-sha <sha> --runner <text> \
        --recorded-on 2026-11-03

The file system is read and written here, the handler decides.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_test_durations_refresh_effect.container import (
    container_test_durations_refresh_effect,
)
from omnimarket.nodes.node_test_durations_refresh_effect.models.model_test_durations_refresh import (
    EnumTestDurationsOperation,
    ModelTestDurationsRefreshRequest,
    ModelTestDurationsRefreshResult,
)

DEFAULT_RECORD = Path("config/test_file_durations.json")
DEFAULT_PROVENANCE = Path("config/test_file_durations_provenance.json")
TESTS_DIR = "tests"
CONTRACT = Path(__file__).resolve().parent / "contract.yaml"
# pytest's default python_files and norecursedirs, the rules that
# scripts/ci/merge_test_durations.py discover_test_files applies; a parity test
# pins the two to the same answer on this tree.
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


def contract_refresh_at() -> int:
    """The unrecorded-file count at which the refresh opens its PR, from the contract."""
    declared = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["refresh"]
    return int(declared["unrecorded_refresh_at"])


def discover_test_files(root: Path) -> tuple[str, ...]:
    """Test files pytest would collect under ``root/tests``, sorted."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root / TESTS_DIR):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not any(fnmatch.fnmatchcase(name, pat) for pat in NORECURSE_PATTERNS)
        )
        for name in filenames:
            if any(fnmatch.fnmatchcase(name, pat) for pat in PYTHON_FILE_PATTERNS):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
    return tuple(sorted(found))


def _read_float_map(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} did not contain a JSON object of id -> duration")
    return {str(key): float(value) for key, value in data.items()}


def _run(request: ModelTestDurationsRefreshRequest) -> ModelTestDurationsRefreshResult:
    handler = (
        container_test_durations_refresh_effect.build_handler_test_durations_refresh()
    )
    return handler.handle(request)


def _status(args: argparse.Namespace) -> int:
    result = _run(
        ModelTestDurationsRefreshRequest(
            operation=EnumTestDurationsOperation.STATUS,
            test_files=discover_test_files(args.root),
            record=_read_float_map(args.record),
            refresh_at=args.refresh_at,
        )
    )
    if args.files_out is not None:
        args.files_out.write_text(
            "".join(f"{name}\n" for name in result.unrecorded), encoding="utf-8"
        )
    sys.stdout.write(
        json.dumps(
            {
                "due": result.due,
                "refresh_at": result.refresh_at,
                "test_files": result.test_file_count,
                "unrecorded": len(result.unrecorded),
            },
            sort_keys=True,
        )
        + "\n"
    )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"due={str(result.due).lower()}\n")
            handle.write(f"unrecorded={len(result.unrecorded)}\n")
    return 0


def _supplement(args: argparse.Namespace) -> int:
    result = _run(
        ModelTestDurationsRefreshRequest(
            operation=EnumTestDurationsOperation.SUPPLEMENT,
            test_files=discover_test_files(args.root),
            record=_read_float_map(args.record),
            refresh_at=args.refresh_at,
            measured=_read_float_map(args.durations),
            ticket=args.ticket,
            recorded_on=args.recorded_on,
            runner=args.runner,
            base_sha=args.base_sha,
        )
    )
    if result.updated_record is None or result.provenance_entry is None:
        sys.stdout.write("no unrecorded test files; record unchanged\n")
        return 0
    args.record.write_text(
        json.dumps(result.updated_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
    provenance[result.provenance_key] = result.provenance_entry.model_dump()
    args.provenance.write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    sys.stdout.write(
        f"recorded {len(result.measured_files)} measured and "
        f"{len(result.unmeasured_files)} unmeasured test files into {args.record}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, helptext in (
        (
            "status",
            _status,
            "count the unrecorded test files; say whether a refresh is due",
        ),
        ("supplement", _supplement, "add the unrecorded files, measured by one run"),
    ):
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("--record", type=Path, default=DEFAULT_RECORD)
        sub.add_argument("--root", type=Path, default=Path.cwd())
        sub.add_argument("--refresh-at", type=int, default=contract_refresh_at())
        if name == "status":
            sub.add_argument(
                "--files-out",
                type=Path,
                help="write the unrecorded files, one per line",
            )
            sub.add_argument(
                "--github-output", type=Path, help="append due= and unrecorded= here"
            )
        else:
            sub.add_argument("--durations", type=Path, required=True)
            sub.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
            sub.add_argument("--base-sha", required=True)
            sub.add_argument("--runner", required=True)
            sub.add_argument("--recorded-on", required=True)
            sub.add_argument("--ticket", default="OMN-22389")
        sub.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    return int(args.handler(args))


__all__ = ["contract_refresh_at", "discover_test_files", "main"]
