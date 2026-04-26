#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Market node drift validator.

Validates that every new or modified node directory is correctly wired:
  1. contract.yaml exists
  2. contract.yaml is valid YAML with required fields (name, node_type, handler)
  3. Node has an entry in pyproject.toml [project.entry-points."onex.nodes"]
  4. Topics declared in contract.yaml event_bus block appear in handler source (best-effort)

Exit codes:
  0 — all checks passed (or only pre-existing WARN-mode violations found)
  1 — one or more FAIL violations found

Flags:
  --check-all             Validate every node_* directory (used locally)
  --check-changed <ref>   Validate only nodes modified since <ref> (used by CI)
  --strict                Promote WARN violations to FAIL (use with --check-changed)
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

NODES_DIR = Path("src/omnimarket/nodes")
PYPROJECT = Path("pyproject.toml")

# All contracts must have at least these fields.
REQUIRED_CONTRACT_FIELDS_ALWAYS = {"name", "node_type"}

# Classic node types (compute/effect/reducer) must also declare a handler block.
# Workflow-package types (workflow, *_GENERIC, orchestrator variants) use
# handler_routing instead and are not required to have a handler block.
NODE_TYPES_REQUIRING_HANDLER = {"compute", "effect", "reducer"}

# Nodes with pre-existing violations on main as of 2026-04-25 (F0 audit OMN-9718 + F3 scan).
# These receive WARN (not FAIL) in non-strict mode so --check-all exits 0 on current main.
# In --strict mode (used by CI on changed nodes) these nodes FAIL if they are touched.
# Remove entries here once the underlying violation is repaired.
#
# 4 nodes missing pyproject entry (OMN-9718 F0):
#   node_full_triage_orchestrator, node_overseer_observer,
#   node_routing_policy_engine, node_state_persist_effect
#
# 23 compute/effect/reducer nodes missing handler block (F3 scan):
KNOWN_MAIN_VIOLATIONS: set[str] = {
    # Missing pyproject entry
    "node_full_triage_orchestrator",
    "node_overseer_observer",
    "node_routing_policy_engine",
    "node_state_persist_effect",
    # Missing handler block (compute/effect/reducer type but no handler declared)
    "node_agent_learning_retrieval_effect",
    "node_build_dispatch_effect",
    "node_intent_query_effect",
    "node_intent_storage_effect",
    "node_loop_state_reducer",
    "node_memory_retrieval_effect",
    "node_memory_storage_effect",
    "node_monitor_alert_responder",
    "node_navigation_history_reducer",
    "node_persona_builder_compute",
    "node_persona_retrieval_effect",
    "node_persona_storage_effect",
    "node_pr_lifecycle_fix_effect",
    "node_pr_lifecycle_merge_effect",
    "node_pr_lifecycle_state_reducer",
    "node_semantic_analyzer_compute",
    "node_similarity_compute",
}


@dataclass
class NodeFinding:
    node: str
    check: str
    level: str  # "FAIL" | "WARN"
    message: str


@dataclass
class NodeResult:
    node: str
    findings: list[NodeFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.level == "FAIL" for f in self.findings)

    @property
    def has_warn(self) -> bool:
        return any(f.level == "WARN" for f in self.findings)


def _load_entry_points(pyproject: Path) -> set[str]:
    """Parse [project.entry-points."onex.nodes"] from pyproject.toml."""
    content = pyproject.read_text()
    m = re.search(
        r'\[project\.entry-points\."onex\.nodes"\](.*?)(?=\n\[|\Z)',
        content,
        re.DOTALL,
    )
    if not m:
        return set()
    entries: set[str] = set()
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            entries.add(line.split("=")[0].strip())
    return entries


def _get_changed_nodes(git_ref: str) -> tuple[list[Path], set[str]]:
    """Return (node directories to validate, set of directly-modified node names).

    - First element: every node directory we should validate this run.
    - Second element: names of nodes whose own source tree was directly modified
      in this diff. Only these nodes are eligible for ``--strict`` promotion of
      WARN→FAIL on pre-existing violations (``KNOWN_MAIN_VIOLATIONS``).

    Raises SystemExit if git diff fails (shallow clone, bad ref, etc.) so the
    gate fails closed rather than silently passing with an empty node list.

    Pyproject behaviour: when ``pyproject.toml`` changes, all node dirs are
    included so a removed entry-point can be caught even without source edits.
    Those pyproject-only audit nodes are NOT marked directly-modified, so they
    do not get strict mode and the ``KNOWN_MAIN_VIOLATIONS`` allowlist still
    applies. Without this distinction, any PR that touches pyproject.toml
    instantly fails the gate on every pre-existing main violation.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-only", git_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"node-drift-gate: git diff failed (exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    changed_files = proc.stdout.strip().splitlines()
    pyproject_changed = any(Path(f).name == "pyproject.toml" for f in changed_files)

    # Always: nodes whose own source tree was directly modified -> eligible for strict.
    directly_modified: set[str] = set()
    for f in changed_files:
        parts = Path(f).parts
        # Match src/omnimarket/nodes/node_*/...
        if (
            len(parts) >= 4
            and parts[0] == "src"
            and parts[1] == "omnimarket"
            and parts[2] == "nodes"
            and parts[3].startswith("node_")
        ):
            directly_modified.add(parts[3])

    if pyproject_changed:
        # Audit all nodes (so a removed entry-point is caught), but only the
        # directly-modified subset is strict-eligible.
        all_node_dirs = sorted(
            p for p in NODES_DIR.iterdir() if p.is_dir() and p.name.startswith("node_")
        )
        return all_node_dirs, directly_modified

    nodes: list[Path] = []
    for name in sorted(directly_modified):
        node_dir = NODES_DIR / name
        if node_dir.is_dir():
            nodes.append(node_dir)
    return nodes, directly_modified


def _extract_contract_topics(contract: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (subscribe_topics, publish_topics) from contract event_bus block."""
    event_bus = contract.get("event_bus", {}) or {}
    subscribe = event_bus.get("subscribe_topics", []) or []
    publish = event_bus.get("publish_topics", []) or []
    return list(subscribe), list(publish)


def _find_handler_source_files(node_dir: Path) -> list[Path]:
    """Return all .py files in the handlers subdirectory of a node."""
    handlers_dir = node_dir / "handlers"
    if not handlers_dir.is_dir():
        return []
    return list(handlers_dir.rglob("*.py"))


def _topic_appears_in_source(topic: str, source_files: list[Path]) -> bool:
    """Best-effort check: does the topic string appear in any handler source file."""
    for src in source_files:
        try:
            if topic in src.read_text():
                return True
        except OSError:
            pass
    return False


def validate_node(
    node_dir: Path,
    entry_points: set[str],
    *,
    strict: bool = False,
) -> NodeResult:
    node_name = node_dir.name
    result = NodeResult(node=node_name)

    # Determine if this node is a known pre-existing violation (WARN vs FAIL)
    is_known_violation = node_name in KNOWN_MAIN_VIOLATIONS

    def add(check: str, message: str, *, fail: bool = True) -> None:
        level = "FAIL" if (fail and (strict or not is_known_violation)) else "WARN"
        result.findings.append(
            NodeFinding(node=node_name, check=check, level=level, message=message)
        )

    # Check 1: contract.yaml exists
    contract_path = node_dir / "contract.yaml"
    if not contract_path.exists():
        add("contract_exists", "contract.yaml is missing")
        return result  # skip remaining checks — no contract to parse

    # Check 2: contract.yaml is valid YAML with required fields
    try:
        contract: dict[str, Any] = yaml.safe_load(contract_path.read_text()) or {}
    except yaml.YAMLError as exc:
        add("contract_valid", f"contract.yaml failed to parse: {exc}")
        return result

    missing_always = REQUIRED_CONTRACT_FIELDS_ALWAYS - set(contract.keys())
    if missing_always:
        add(
            "contract_fields",
            f"contract.yaml missing required fields: {sorted(missing_always)}",
        )

    # handler is required only for classic node types (compute/effect/reducer)
    node_type = str(contract.get("node_type", "")).lower()
    if node_type in NODE_TYPES_REQUIRING_HANDLER and "handler" not in contract:
        add(
            "contract_fields",
            f"contract.yaml missing 'handler' block (required for node_type={node_type!r})",
        )

    # Check 3: pyproject.toml entry point
    if node_name not in entry_points:
        add(
            "pyproject_entry",
            f'no entry in pyproject.toml [project.entry-points."onex.nodes"] for {node_name}',
        )

    # Check 4: topic-handler alignment (best-effort, always WARN not FAIL)
    subscribe_topics, _publish_topics = _extract_contract_topics(contract)
    if subscribe_topics:
        source_files = _find_handler_source_files(node_dir)
        for topic in subscribe_topics:
            if not _topic_appears_in_source(topic, source_files):
                result.findings.append(
                    NodeFinding(
                        node=node_name,
                        check="topic_handler_alignment",
                        level="WARN",
                        message=f"subscribe topic '{topic}' not found in handler source files (best-effort)",
                    )
                )

    return result


def collect_nodes(*, changed_ref: str | None) -> tuple[list[Path], set[str] | None]:
    """Return (nodes to validate, set of strict-eligible node names | None).

    If ``changed_ref`` is None we are in --check-all mode; the strict-eligible
    set is None meaning "use the strict flag uniformly for every node".
    """
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
    entry_points = _load_entry_points(PYPROJECT)
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
            print("node-drift-gate: no node directories to validate — PASS")
        return 0

    results: list[NodeResult] = []
    for node_dir in nodes:
        # Strict applies uniformly when strict_eligible is None (full --check-all).
        # When we have a directly-modified set, strict only applies to those nodes;
        # other nodes (audited because pyproject.toml changed) use WARN-mode so the
        # KNOWN_MAIN_VIOLATIONS allowlist still suppresses pre-existing-on-main noise.
        node_strict = strict and (
            strict_eligible is None or node_dir.name in strict_eligible
        )
        results.append(validate_node(node_dir, entry_points, strict=node_strict))

    fail_results = [r for r in results if not r.passed]
    warn_results = [r for r in results if r.passed and r.has_warn]
    ok_results = [r for r in results if r.passed and not r.has_warn]

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
                    "status": "fail"
                    if not r.passed
                    else ("warn" if r.has_warn else "ok"),
                    "findings": [
                        {"check": f.check, "level": f.level, "message": f.message}
                        for f in r.findings
                    ],
                }
                for r in results
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        mode = "strict" if strict else ("changed-only" if changed_ref else "full")
        print(f"node-drift-gate ({mode} mode): {len(nodes)} node(s) checked")
        print(
            f"  PASS: {len(ok_results)}  WARN: {len(warn_results)}  FAIL: {len(fail_results)}"
        )

        for r in warn_results:
            for f in r.findings:
                print(f"  [{f.level}] {r.node} / {f.check}: {f.message}")

        for r in fail_results:
            for f in r.findings:
                print(f"  [{f.level}] {r.node} / {f.check}: {f.message}")

        if fail_results:
            print("\nnode-drift-gate: FAIL")
        else:
            print("\nnode-drift-gate: PASS")

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
        help="promote WARN violations to FAIL (use with --check-changed in CI)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="output_json", help="output JSON"
    )
    args = parser.parse_args()

    changed_ref = args.check_changed if not args.check_all else None
    return run(
        changed_ref=changed_ref, strict=args.strict, output_json=args.output_json
    )


if __name__ == "__main__":
    raise SystemExit(main())
