#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Market contract-state-coverage gate (OMN-13781).

Extracts each node's declared state set from ``contract.yaml``:
  - FSM states/transitions for orchestrators (``fsm.states`` or
    ``state_machine.states[].state_name``)
  - Output classes / event types for compute/effect/reducer nodes (keys of
    the ``outputs`` block plus ``event_bus.publish_topics``)

...and diffs it against the states asserted by that node's tests (a state is
"covered" when its literal name appears as a quoted string or attribute
access anywhere in the node's associated test files). Any node with an
uncovered declared state FAILS the gate.

Pre-existing gaps as of 2026-07-01 are catalogued in
``scripts/validation/state_coverage_baseline.txt`` and are WARN, not FAIL,
in non-strict mode. In ``--strict`` mode (used by CI on changed nodes) a
baselined node still FAILs if it is directly touched by the diff — the
baseline is a grandfather clause for untouched legacy debt, not a permanent
exemption.

Exit codes:
  0 — all checks passed (or only pre-existing baselined violations found)
  1 — one or more FAIL violations found

Flags:
  --check-all             Validate every node_* directory (used locally)
  --check-changed <ref>   Validate only nodes modified since <ref> (used by CI)
  --strict                Promote baselined WARN violations to FAIL for
                           directly-modified nodes (use with --check-changed)
  --json                  Output machine-readable JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
TESTS_DIR = REPO_ROOT / "tests"
PYPROJECT = REPO_ROOT / "pyproject.toml"
BASELINE_PATH = REPO_ROOT / "scripts" / "validation" / "state_coverage_baseline.txt"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass
class DeclaredStates:
    kind: str  # "fsm" | "outputs"
    states: list[str] = field(default_factory=list)


def _load_baseline() -> set[tuple[str, str]]:
    """Load ``(node_name, state)`` pairs pre-existing on main.

    File format: one ``<node_name> <state>`` pair per line; ``#`` starts a
    comment; blank lines are ignored.
    """
    if not BASELINE_PATH.exists():
        return set()
    pairs: set[tuple[str, str]] = set()
    for raw_line in BASELINE_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pairs.add((parts[0], parts[1]))
    return pairs


def _extract_fsm_states(contract: dict[str, Any]) -> list[str] | None:
    """Return FSM state names if the contract declares an FSM block."""
    fsm = contract.get("fsm")
    if isinstance(fsm, dict):
        raw_states = fsm.get("states")
        if isinstance(raw_states, list):
            names = [s for s in raw_states if isinstance(s, str)]
            if names:
                return names

    state_machine = contract.get("state_machine")
    if isinstance(state_machine, dict):
        raw_states = state_machine.get("states")
        if isinstance(raw_states, list):
            names = []
            for entry in raw_states:
                if isinstance(entry, dict):
                    name = entry.get("state_name")
                    if isinstance(name, str):
                        names.append(name)
                elif isinstance(entry, str):
                    names.append(entry)
            if names:
                return names

    return None


def _extract_output_states(contract: dict[str, Any]) -> list[str]:
    """Return output-class field names + published event/topic types."""
    states: list[str] = []

    outputs = contract.get("outputs")
    if isinstance(outputs, dict):
        states.extend(str(k) for k in outputs if isinstance(k, str))

    event_bus = contract.get("event_bus")
    if isinstance(event_bus, dict):
        publish = event_bus.get("publish_topics")
        if isinstance(publish, list):
            states.extend(str(t) for t in publish if isinstance(t, str))

    terminal_event = contract.get("terminal_event")
    if isinstance(terminal_event, str):
        states.append(terminal_event)

    # De-dup while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in states:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def declared_states(contract: dict[str, Any]) -> DeclaredStates:
    fsm_states = _extract_fsm_states(contract)
    if fsm_states is not None:
        return DeclaredStates(kind="fsm", states=fsm_states)
    return DeclaredStates(kind="outputs", states=_extract_output_states(contract))


def _load_test_corpus() -> list[tuple[str, str]]:
    """Read every test file once: [(posix_path, content), ...].

    Reading each file exactly once (instead of per-node) keeps the gate
    O(files) rather than O(nodes * files) across ~350 nodes / ~1100 tests.
    """
    if not TESTS_DIR.exists():
        return []
    corpus: list[tuple[str, str]] = []
    for test_file in TESTS_DIR.rglob("*.py"):
        try:
            content = test_file.read_text(errors="ignore")
        except OSError:
            continue
        corpus.append((test_file.as_posix(), content))
    return corpus


def _node_test_content(node_name: str, corpus: list[tuple[str, str]]) -> str:
    """Concatenated content of every test file associated with a node.

    A test file is associated with a node when the node name appears in its
    own path, OR the file's content references the node's module path
    (``omnimarket.nodes.<node_name>``) or the bare node name (covers
    dynamic-import / fixture-string references).
    """
    parts: list[str] = []
    for path, content in corpus:
        if node_name in path or node_name in content:
            parts.append(content)
    return "\n".join(parts)


def _state_covered(state: str, test_contents: str) -> bool:
    """A declared state is covered if it is literally asserted in a test."""
    if not state:
        return False
    # Quoted string literal (covers topic strings, FSM state names, dict keys).
    quoted_pattern = re.compile(r"""['"]""" + re.escape(state) + r"""['"]""")
    if quoted_pattern.search(test_contents):
        return True
    # Bare attribute access (covers Pydantic model field assertions like
    # ``result.overall_status``). Only meaningful for identifier-shaped states
    # (topics/event types contain '.' and would false-positive as substrings).
    if _IDENTIFIER_RE.match(state) and "." not in state:
        attr_pattern = re.compile(r"\." + re.escape(state) + r"\b")
        if attr_pattern.search(test_contents):
            return True
    return False


@dataclass
class NodeCoverageResult:
    node: str
    kind: str
    declared: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    baselined_uncovered: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.uncovered


def validate_node(
    node_dir: Path,
    *,
    baseline: set[tuple[str, str]],
    strict: bool,
    test_corpus: list[tuple[str, str]],
) -> NodeCoverageResult:
    node_name = node_dir.name
    contract_path = node_dir / "contract.yaml"
    if not contract_path.exists():
        return NodeCoverageResult(node=node_name, kind="none")

    try:
        contract = yaml.safe_load(contract_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return NodeCoverageResult(
            node=node_name, kind="error", uncovered=[f"contract-parse-error: {exc}"]
        )
    if not isinstance(contract, dict):
        return NodeCoverageResult(node=node_name, kind="none")

    ds = declared_states(contract)
    if not ds.states:
        return NodeCoverageResult(node=node_name, kind=ds.kind)

    combined_content = _node_test_content(node_name, test_corpus)

    raw_uncovered = [s for s in ds.states if not _state_covered(s, combined_content)]

    uncovered: list[str] = []
    baselined_uncovered: list[str] = []
    for state in raw_uncovered:
        is_baselined = (node_name, state) in baseline
        # Baselined pre-existing gaps stay WARN unless strict mode promotes
        # them — strict is only True here for nodes the caller marked
        # strict-eligible (directly modified in the diff), so untouched
        # legacy debt keeps its grandfather clause.
        if is_baselined and not strict:
            baselined_uncovered.append(state)
        else:
            uncovered.append(state)

    return NodeCoverageResult(
        node=node_name,
        kind=ds.kind,
        declared=ds.states,
        uncovered=uncovered,
        baselined_uncovered=baselined_uncovered,
    )


def _get_changed_nodes(git_ref: str) -> tuple[list[Path], set[str]]:
    """Return (node directories to validate, directly-modified node names)."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", git_ref],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        print(
            f"state-coverage-gate: git diff failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    changed_files = proc.stdout.strip().splitlines()
    directly_modified: set[str] = set()
    for f in changed_files:
        parts = Path(f).parts
        if (
            len(parts) >= 4
            and parts[0] == "src"
            and parts[1] == "omnimarket"
            and parts[2] == "nodes"
            and parts[3].startswith("node_")
        ):
            directly_modified.add(parts[3])
        # A node's own tests being touched should also re-check its coverage.
        if len(parts) >= 1 and parts[0] == "tests" and NODES_DIR.exists():
            for candidate in NODES_DIR.iterdir():
                if candidate.is_dir() and candidate.name in Path(f).as_posix():
                    directly_modified.add(candidate.name)

    nodes: list[Path] = []
    for name in sorted(directly_modified):
        node_dir = NODES_DIR / name
        if node_dir.is_dir():
            nodes.append(node_dir)
    return nodes, directly_modified


def collect_nodes(*, changed_ref: str | None) -> tuple[list[Path], set[str] | None]:
    if changed_ref is not None:
        nodes, directly_modified = _get_changed_nodes(changed_ref)
        return nodes, directly_modified
    all_nodes = sorted(
        p for p in NODES_DIR.iterdir() if p.is_dir() and p.name.startswith("node_")
    )
    return all_nodes, None


def run(
    *,
    changed_ref: str | None,
    strict: bool,
    output_json: bool,
) -> int:
    baseline = _load_baseline()
    nodes, strict_eligible = collect_nodes(changed_ref=changed_ref)

    if not nodes:
        msg = {
            "status": "ok",
            "message": "no node directories to validate",
            "results": [],
        }
        if output_json:
            print(json.dumps(msg))
        else:
            print("state-coverage-gate: no node directories to validate — PASS")
        return 0

    test_corpus = _load_test_corpus()
    results: list[NodeCoverageResult] = []
    for node_dir in nodes:
        node_strict = strict and (
            strict_eligible is None or node_dir.name in strict_eligible
        )
        results.append(
            validate_node(
                node_dir,
                baseline=baseline,
                strict=node_strict,
                test_corpus=test_corpus,
            )
        )

    fail_results = [r for r in results if not r.passed]
    warn_results = [r for r in results if r.passed and r.baselined_uncovered]
    ok_results = [r for r in results if r.passed and not r.baselined_uncovered]

    if output_json:
        output: dict[str, Any] = {
            "status": "fail" if fail_results else "ok",
            "summary": {
                "total": len(results),
                "failed": len(fail_results),
                "warned": len(warn_results),
                "passed": len(ok_results),
            },
            "results": [
                {
                    "node": r.node,
                    "kind": r.kind,
                    "declared": r.declared,
                    "uncovered": r.uncovered,
                    "baselined_uncovered": r.baselined_uncovered,
                }
                for r in results
                if r.uncovered or r.baselined_uncovered
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        mode = "strict" if strict else ("changed-only" if changed_ref else "full")
        print(f"state-coverage-gate ({mode} mode): {len(nodes)} node(s) checked")
        print(
            f"  PASS: {len(ok_results)}  WARN: {len(warn_results)}  "
            f"FAIL: {len(fail_results)}"
        )
        for r in warn_results:
            print(
                f"  [WARN] {r.node}: baselined uncovered states: "
                f"{r.baselined_uncovered}"
            )
        for r in fail_results:
            print(f"  [FAIL] {r.node} ({r.kind}): uncovered states: {r.uncovered}")

        if fail_results:
            print("\nstate-coverage-gate: FAIL")
        else:
            print("\nstate-coverage-gate: PASS")

    return 1 if fail_results else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-all", action="store_true", help="validate all nodes")
    mode.add_argument(
        "--check-changed",
        metavar="GIT_REF",
        help="validate only nodes changed since GIT_REF (e.g. origin/main)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "promote baselined WARN violations to FAIL for directly-modified "
            "nodes (use with --check-changed in CI)"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", dest="output_json", help="output JSON"
    )
    args = parser.parse_args()

    changed_ref = args.check_changed if not args.check_all else None
    return run(
        changed_ref=changed_ref,
        strict=args.strict,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
