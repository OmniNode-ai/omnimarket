#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Block deletion of flow-scoped tests without a PASS parity receipt.

Flow declarations live at
``validation/chain_replacement_receipts/<flow>.inputs.yaml``.  Scope patterns
are matched against POSIX repository paths.  Python 3.13's
``PurePosixPath.full_match`` supplies recursive ``**`` semantics when present.
On Python 3.12, the fallback replaces ``**`` with ``*`` before using
``fnmatchcase``; ``fnmatch`` stars already cross ``/``, so this preserves the
recursive meaning of ``**`` on POSIX paths.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from ci.chain_replacement_parity import CheckId, ParityReceipt
else:
    from chain_replacement_parity import CheckId, ParityReceipt

_RECEIPT_DIR = "validation/chain_replacement_receipts"
_INPUTS_SUFFIX = ".inputs.yaml"
_CHECK_IDS: tuple[CheckId, ...] = ("P1", "P2", "P3", "P4", "P5")
_GATE_NAME = "test-deletion-parity-receipt-gate"

Mode = Literal["changed-ref", "staged"]
Status = Literal["PASS", "FAIL", "SKIP"]


class GateError(RuntimeError):
    """An operational error that prevents a trustworthy gate result."""


@dataclass(frozen=True)
class GitSnapshot:
    """Git objects used as the before and after sides of a diff."""

    mode: Mode
    base_rev: str
    head_rev: str


@dataclass(frozen=True)
class ChangedTest:
    """A test file's base and head locations."""

    base_path: str
    head_path: str | None

    @property
    def result_path(self) -> str:
        return self.head_path or self.base_path


@dataclass(frozen=True)
class DeletedCase:
    """One pytest case removed between the two snapshots."""

    case_id: str
    path: str


@dataclass(frozen=True)
class Result:
    """One flow evaluation, or one explicit out-of-scope skip."""

    case: str
    flow: str | None
    status: Status
    reason: str


def _run_git(*args: str, allow_missing: bool = False) -> str | None:
    process = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    if process.returncode == 0:
        return process.stdout
    if allow_missing:
        return None
    detail = process.stderr.strip() or process.stdout.strip()
    raise GateError(
        f"git {' '.join(args)} failed with exit {process.returncode}: {detail}"
    )


def _snapshot(*, changed_ref: str | None, staged: bool) -> GitSnapshot:
    if staged:
        return GitSnapshot(mode="staged", base_rev="HEAD", head_rev=":")
    if changed_ref is None:
        raise GateError("one of --changed-ref or --staged is required")
    merge_base = _run_git("merge-base", changed_ref, "HEAD")
    assert merge_base is not None
    return GitSnapshot(
        mode="changed-ref",
        base_rev=merge_base.strip(),
        head_rev="HEAD",
    )


def _diff_lines(snapshot: GitSnapshot, changed_ref: str | None) -> list[str]:
    if snapshot.mode == "staged":
        output = _run_git("diff", "--cached", "--name-status", "-M", "HEAD")
    else:
        assert changed_ref is not None
        output = _run_git("diff", "--name-status", "-M", f"{changed_ref}...HEAD")
    assert output is not None
    return [line for line in output.splitlines() if line]


def _is_test_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        len(candidate.parts) >= 2
        and candidate.parts[0] == "tests"
        and candidate.name.startswith("test_")
        and candidate.suffix == ".py"
    )


def _changed_tests(
    snapshot: GitSnapshot,
    changed_ref: str | None,
) -> list[ChangedTest]:
    changed: list[ChangedTest] = []
    for line in _diff_lines(snapshot, changed_ref):
        fields = line.split("\t")
        status = fields[0]
        kind = status[0]
        if kind in {"R", "C"} and len(fields) == 3:
            old_path, new_path = fields[1:]
            if _is_test_path(old_path) or _is_test_path(new_path):
                changed.append(ChangedTest(old_path, new_path))
        elif len(fields) == 2:
            path = fields[1]
            if not _is_test_path(path) or kind == "A":
                continue
            changed.append(ChangedTest(path, None if kind == "D" else path))
    return changed


def _read_git_file(rev: str, path: str) -> str | None:
    object_name = f":{path}" if rev == ":" else f"{rev}:{path}"
    return _run_git("show", object_name, allow_missing=True)


def _case_suffixes(source: str, *, path: str, side: str) -> set[str]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise GateError(f"cannot parse {side} test file {path}: {exc.msg}") from exc

    cases: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                cases.add(node.name)
            continue
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                child.name.startswith("test_")
            ):
                cases.add(f"{node.name}::{child.name}")
    return cases


def _deleted_cases(
    snapshot: GitSnapshot,
    changed_ref: str | None,
) -> list[DeletedCase]:
    deleted: list[DeletedCase] = []
    for changed in _changed_tests(snapshot, changed_ref):
        base_source = _read_git_file(snapshot.base_rev, changed.base_path)
        if base_source is None:
            raise GateError(
                f"cannot read base test file {changed.base_path} "
                f"from {snapshot.base_rev}"
            )
        base_cases = _case_suffixes(
            base_source,
            path=changed.base_path,
            side="base",
        )
        head_cases: set[str] = set()
        if changed.head_path is not None:
            head_source = _read_git_file(snapshot.head_rev, changed.head_path)
            if head_source is not None:
                head_cases = _case_suffixes(
                    head_source,
                    path=changed.head_path,
                    side="head",
                )

        result_path = changed.result_path
        for suffix in sorted(base_cases - head_cases):
            deleted.append(
                DeletedCase(case_id=f"{result_path}::{suffix}", path=result_path)
            )
    return sorted(deleted, key=lambda case: case.case_id)


def _list_declarations(rev: str) -> list[str]:
    if rev == ":":
        output = _run_git("ls-files", "--cached", "--", _RECEIPT_DIR)
    else:
        output = _run_git(
            "ls-tree",
            "-r",
            "--name-only",
            rev,
            "--",
            _RECEIPT_DIR,
        )
    assert output is not None
    return sorted(
        path
        for path in output.splitlines()
        if path.startswith(f"{_RECEIPT_DIR}/") and path.endswith(_INPUTS_SUFFIX)
    )


def _parse_scope(path: str, content: str, side: str) -> list[str]:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise GateError(f"malformed {side} inputs file {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("scope"), list):
        raise GateError(f"malformed {side} inputs file {path}: scope must be a list")
    scope = payload["scope"]
    if not all(isinstance(pattern, str) for pattern in scope):
        raise GateError(
            f"malformed {side} inputs file {path}: scope entries must be strings"
        )
    return [str(pattern) for pattern in scope]


def _flow_name(path: str) -> str:
    return PurePosixPath(path).name.removesuffix(_INPUTS_SUFFIX)


def _flow_scopes(snapshot: GitSnapshot) -> dict[str, tuple[str, ...]]:
    scopes: dict[str, set[str]] = {}
    for side, rev in (("base", snapshot.base_rev), ("head", snapshot.head_rev)):
        for path in _list_declarations(rev):
            content = _read_git_file(rev, path)
            if content is None:
                raise GateError(f"cannot read {side} inputs file {path}")
            flow = _flow_name(path)
            scopes.setdefault(flow, set()).update(_parse_scope(path, content, side))
    return {flow: tuple(sorted(patterns)) for flow, patterns in sorted(scopes.items())}


def _matches_scope(path: str, pattern: str) -> bool:
    candidate = PurePosixPath(path)
    full_match = getattr(candidate, "full_match", None)
    if callable(full_match):
        return bool(full_match(pattern))
    return fnmatch.fnmatchcase(path, pattern.replace("**", "*"))


def _matching_flows(path: str, scopes: dict[str, tuple[str, ...]]) -> list[str]:
    return [
        flow
        for flow, patterns in scopes.items()
        if any(_matches_scope(path, pattern) for pattern in patterns)
    ]


def _receipt_path(flow: str) -> str:
    return f"{_RECEIPT_DIR}/{flow}.json"


def _load_receipt(snapshot: GitSnapshot, flow: str) -> tuple[ParityReceipt | None, str]:
    path = _receipt_path(flow)
    content = _read_git_file(snapshot.head_rev, path)
    if content is None:
        return None, f"no receipt at {path}"
    try:
        receipt = ParityReceipt.model_validate_json(content)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        return None, f"receipt at {path} is not a valid ParityReceipt: {exc}"
    if receipt.flow != flow:
        return None, f"receipt flow is {receipt.flow!r}, expected {flow!r}"
    return receipt, ""


def _evaluate_case(
    case: DeletedCase,
    flow: str,
    receipt: ParityReceipt | None,
    receipt_error: str,
) -> Result:
    if receipt is None:
        return Result(case.case_id, flow, "FAIL", receipt_error)
    if receipt.verdict != "PASS":
        return Result(
            case.case_id,
            flow,
            "FAIL",
            f"receipt verdict is {receipt.verdict}, not PASS",
        )
    for check_id in _CHECK_IDS:
        check = receipt.checks[check_id]
        if check.status != "PASS":
            return Result(
                case.case_id,
                flow,
                "FAIL",
                f"check {check_id} status is {check.status}, not PASS",
            )
    if case.case_id not in receipt.deleted_cases:
        return Result(case.case_id, flow, "FAIL", "case not admitted by receipt")
    return Result(case.case_id, flow, "PASS", "PASS receipt admits deletion")


def _evaluate(
    deleted_cases: list[DeletedCase],
    scopes: dict[str, tuple[str, ...]],
    snapshot: GitSnapshot,
) -> list[Result]:
    results: list[Result] = []
    receipt_cache: dict[str, tuple[ParityReceipt | None, str]] = {}
    for case in deleted_cases:
        flows = _matching_flows(case.path, scopes)
        if not flows:
            results.append(
                Result(
                    case.case_id,
                    None,
                    "SKIP",
                    "not in any chain-replacement flow scope",
                )
            )
            continue
        for flow in flows:
            receipt, error = receipt_cache.setdefault(
                flow,
                _load_receipt(snapshot, flow),
            )
            results.append(_evaluate_case(case, flow, receipt, error))
    return results


def _summary(deleted_cases: list[DeletedCase], results: list[Result]) -> dict[str, int]:
    failed_cases = {result.case for result in results if result.status == "FAIL"}
    skipped_cases = {result.case for result in results if result.status == "SKIP"}
    passed_cases = (
        {case.case_id for case in deleted_cases} - failed_cases - skipped_cases
    )
    return {
        "deleted_cases": len(deleted_cases),
        "passed": len(passed_cases),
        "failed": len(failed_cases),
        "skipped": len(skipped_cases),
    }


def _emit(
    deleted_cases: list[DeletedCase],
    results: list[Result],
    *,
    output_json: bool,
    operational_error: str | None = None,
) -> int:
    summary = _summary(deleted_cases, results)
    failed = bool(summary["failed"] or operational_error)
    if output_json:
        payload: dict[str, Any] = {
            "status": "fail" if failed else "pass",
            "summary": summary,
            "results": [
                {
                    "case": result.case,
                    "flow": result.flow,
                    "status": result.status.lower(),
                    "reason": result.reason,
                }
                for result in results
            ],
        }
        if operational_error is not None:
            payload["error"] = operational_error
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if failed else 0

    if operational_error is not None:
        print(f"{_GATE_NAME}: [FAIL] {operational_error}")
    for result in results:
        if result.flow is None:
            print(f"  [{result.status}] {result.case}: {result.reason}")
        else:
            print(
                f"  [{result.status}] {result.case} (flow {result.flow}): "
                f"{result.reason}"
            )
    print(
        f"{_GATE_NAME}: {'FAIL' if failed else 'PASS'} - "
        f"{summary['deleted_cases']} deleted test case(s), "
        f"{summary['failed']} failed, {summary['skipped']} skipped"
    )
    return 1 if failed else 0


def run(*, changed_ref: str | None, staged: bool, output_json: bool) -> int:
    """Run the deletion gate for one git comparison mode."""

    deleted_cases: list[DeletedCase] = []
    try:
        snapshot = _snapshot(changed_ref=changed_ref, staged=staged)
        scopes = _flow_scopes(snapshot)
        deleted_cases = _deleted_cases(snapshot, changed_ref)
        results = _evaluate(deleted_cases, scopes, snapshot)
    except GateError as exc:
        return _emit(
            deleted_cases,
            [],
            output_json=output_json,
            operational_error=str(exc),
        )
    return _emit(deleted_cases, results, output_json=output_json)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--changed-ref", metavar="REF")
    mode.add_argument("--staged", action="store_true")
    parser.add_argument("--json", action="store_true", dest="output_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    args = _parser().parse_args(argv)
    return run(
        changed_ref=args.changed_ref,
        staged=args.staged,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
