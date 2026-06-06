#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage gate for changed live-path market nodes.

The gate is baseline-friendly: CI and pre-commit enforce only nodes touched by
the current diff, so existing historical gaps do not block unrelated work.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NODES_DIR = Path("src/omnimarket/nodes")
TESTS_DIR = Path("tests")


@dataclass(frozen=True)
class CoverageTarget:
    node_name: str
    node_dir: Path
    handler_tokens: tuple[str, ...]


@dataclass
class CoverageResult:
    node: str
    status: str
    findings: list[str] = field(default_factory=list)
    matched_tests: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status != "fail"


def _run_git_diff(args: list[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            "golden-chain-coverage-gate: git diff failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _changed_files_from_ref(ref: str) -> list[str]:
    return _run_git_diff(["diff", "--name-only", f"{ref}...HEAD"])


def _changed_files_from_staged() -> list[str]:
    return _run_git_diff(["diff", "--cached", "--name-only"])


def _node_names_from_changed_files(changed_files: list[str]) -> set[str]:
    node_names: set[str] = set()
    for raw_path in changed_files:
        parts = Path(raw_path).parts
        if (
            len(parts) >= 4
            and parts[0] == "src"
            and parts[1] == "omnimarket"
            and parts[2] == "nodes"
            and parts[3].startswith("node_")
        ):
            node_names.add(parts[3])
    return node_names


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        print(
            f"golden-chain-coverage-gate: {path} failed to parse: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not isinstance(value, dict):
        return {}
    return value


def _is_live_path_node(node_dir: Path) -> bool:
    metadata_path = node_dir / "metadata.yaml"
    if not metadata_path.exists():
        return True

    metadata = _load_yaml(metadata_path)
    if bool(metadata.get("deprecated", False)):
        return False

    capabilities = metadata.get("capabilities") or {}
    if isinstance(capabilities, dict) and capabilities.get("full_runtime") is False:
        return False

    return True


def _handler_tokens_from_contract(contract: dict[str, Any]) -> tuple[str, ...]:
    tokens: set[str] = set()

    def add_handler(raw_handler: Any) -> None:
        if not isinstance(raw_handler, dict):
            return
        module = raw_handler.get("module") or raw_handler.get("handler_module")
        if isinstance(module, str) and module:
            tokens.add(module)
            tokens.add(module.rsplit(".", maxsplit=1)[-1])
        for key in ("class", "name", "handler_class"):
            value = raw_handler.get(key)
            if isinstance(value, str) and value:
                tokens.add(value)

    add_handler(contract.get("handler"))

    routing = contract.get("handler_routing") or {}
    if isinstance(routing, dict):
        handlers = routing.get("handlers") or []
        if isinstance(handlers, list):
            for raw_route in handlers:
                if not isinstance(raw_route, dict):
                    continue
                add_handler(raw_route)
                add_handler(raw_route.get("handler"))

    return tuple(sorted(tokens))


def _coverage_target_for_node(node_dir: Path) -> CoverageTarget:
    contract_path = node_dir / "contract.yaml"
    if not contract_path.exists():
        return CoverageTarget(
            node_name=node_dir.name, node_dir=node_dir, handler_tokens=()
        )
    contract = _load_yaml(contract_path)
    return CoverageTarget(
        node_name=node_dir.name,
        node_dir=node_dir,
        handler_tokens=_handler_tokens_from_contract(contract),
    )


def _golden_chain_test_files(repo_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    if (repo_root / TESTS_DIR).is_dir():
        candidates.update((repo_root / TESTS_DIR).rglob("test_golden_chain*.py"))
    candidates.update(
        (repo_root / NODES_DIR).glob("node_*/tests/test_golden_chain*.py")
    )
    return sorted(candidates)


def _normalize_node_suffix(node_name: str) -> str:
    return node_name.removeprefix("node_")


def _test_path_matches_node(test_path: Path, target: CoverageTarget) -> bool:
    parts = test_path.parts
    if target.node_name in parts:
        return True
    expected_stem = f"test_golden_chain_{_normalize_node_suffix(target.node_name)}"
    return test_path.stem == expected_stem


def _test_content_matches_node(test_path: Path, target: CoverageTarget) -> bool:
    try:
        content = test_path.read_text(errors="replace")
    except OSError:
        return False

    tokens = (target.node_name, *target.handler_tokens)
    return any(token and token in content for token in tokens)


def _find_matching_golden_chain_tests(
    repo_root: Path,
    target: CoverageTarget,
) -> list[Path]:
    matches: list[Path] = []
    for test_path in _golden_chain_test_files(repo_root):
        if _test_path_matches_node(test_path, target) or _test_content_matches_node(
            test_path, target
        ):
            matches.append(test_path)
    return matches


def collect_targets(
    *,
    changed_ref: str | None,
    staged: bool,
    check_all: bool,
) -> list[CoverageTarget]:
    if check_all:
        node_names = {
            path.name
            for path in NODES_DIR.iterdir()
            if path.is_dir() and path.name.startswith("node_")
        }
    elif staged:
        node_names = _node_names_from_changed_files(_changed_files_from_staged())
    elif changed_ref is not None:
        node_names = _node_names_from_changed_files(
            _changed_files_from_ref(changed_ref)
        )
    else:
        raise ValueError("one of changed_ref, staged, or check_all is required")

    targets: list[CoverageTarget] = []
    for node_name in sorted(node_names):
        node_dir = NODES_DIR / node_name
        if not node_dir.is_dir():
            continue
        if not _is_live_path_node(node_dir):
            continue
        targets.append(_coverage_target_for_node(node_dir))
    return targets


def run(
    *, changed_ref: str | None, staged: bool, check_all: bool, output_json: bool
) -> int:
    repo_root = Path.cwd()
    targets = collect_targets(
        changed_ref=changed_ref, staged=staged, check_all=check_all
    )

    if not targets:
        payload = {
            "status": "ok",
            "message": "no changed live-path node directories to validate",
            "results": [],
        }
        if output_json:
            print(json.dumps(payload))
        else:
            print(
                "golden-chain-coverage-gate: no changed live-path node directories "
                "to validate - PASS"
            )
        return 0

    results: list[CoverageResult] = []
    for target in targets:
        matches = _find_matching_golden_chain_tests(repo_root, target)
        if matches:
            results.append(
                CoverageResult(
                    node=target.node_name,
                    status="ok",
                    matched_tests=[str(path) for path in matches],
                )
            )
        else:
            results.append(
                CoverageResult(
                    node=target.node_name,
                    status="fail",
                    findings=[
                        "changed live-path node has no matching golden-chain test",
                        (
                            "add tests/test_golden_chain_<node>.py, "
                            "tests/nodes/<node>/test_golden_chain_*.py, or "
                            "<node>/tests/test_golden_chain*.py"
                        ),
                    ],
                )
            )

    failed = [result for result in results if not result.passed]
    if output_json:
        print(
            json.dumps(
                {
                    "status": "fail" if failed else "ok",
                    "summary": {
                        "total": len(results),
                        "failed": len(failed),
                        "passed": len(results) - len(failed),
                    },
                    "results": [
                        {
                            "node": result.node,
                            "status": result.status,
                            "findings": result.findings,
                            "matched_tests": result.matched_tests,
                        }
                        for result in results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"golden-chain-coverage-gate: {len(results)} live-path node(s) checked")
        for result in results:
            if result.passed:
                print(f"  [PASS] {result.node}: {', '.join(result.matched_tests)}")
            else:
                for finding in result.findings:
                    print(f"  [FAIL] {result.node}: {finding}")
        print(f"\ngolden-chain-coverage-gate: {'FAIL' if failed else 'PASS'}")

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--changed-ref",
        metavar="GIT_REF",
        help="validate live-path nodes changed since GIT_REF",
    )
    mode.add_argument(
        "--staged",
        action="store_true",
        help="validate live-path nodes staged for commit",
    )
    mode.add_argument(
        "--check-all",
        action="store_true",
        help="validate every live-path node",
    )
    parser.add_argument("--json", action="store_true", dest="output_json")
    args = parser.parse_args()
    return run(
        changed_ref=args.changed_ref,
        staged=args.staged,
        check_all=args.check_all,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
