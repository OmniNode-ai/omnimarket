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
import ast
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

# OMN-14151: a node explicitly marked lifecycle/status: deprecated or
# experimental is intentionally not getting further test investment (e.g. a
# hard-gated legacy surface retired in favor of a replacement node) — its
# baselined gaps stay WARN even when the node is directly touched (a hard-gate
# neuters the node without exercising its declared states). Mirrors the same
# exemption already applied to the pyproject-entry check in
# scripts/validate_node_drift.py.
LIFECYCLE_EXEMPTIONS = {"deprecated", "experimental"}

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


def _contract_lifecycle(contract: dict[str, Any]) -> str:
    """Return normalized contract lifecycle/status annotation, if present."""
    candidates: list[Any] = [contract.get("lifecycle"), contract.get("status")]
    for nested_key in ("metadata", "descriptor"):
        nested = contract.get(nested_key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("lifecycle"), nested.get("status")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return ""


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


def _node_test_sources(node_name: str, corpus: list[tuple[str, str]]) -> list[str]:
    """Per-file source of every test file associated with a node.

    A test file is associated with a node when the node name appears in its
    own path, OR the file's content references the node's module path
    (``omnimarket.nodes.<node_name>``) or the bare node name (covers
    dynamic-import / fixture-string references).

    Returned per file (not concatenated): each real test file is a
    self-contained module, but concatenating several — each carrying its own
    ``from __future__ import annotations`` — raises ``SyntaxError`` on a single
    ``ast.parse`` (a ``__future__`` import must be the first statement).
    """
    sources: list[str] = []
    for path, content in corpus:
        if node_name in path or node_name in content:
            sources.append(content)
    return sources


def _state_match_nodes(
    tree: ast.AST, state: str, *, identifier_state: bool
) -> list[ast.AST]:
    """AST nodes that reference ``state`` — a string ``Constant`` equal to it,
    or (for identifier-shaped states) an ``Attribute`` whose ``.attr`` is it.

    Attribute references cover the common enum idiom
    (``EnumOrchestratorState.INVENTORYING``); string constants cover topic
    strings, dict keys, and quoted FSM names.
    """
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and node.value == state) or (
            identifier_state and isinstance(node, ast.Attribute) and node.attr == state
        ):
            matches.append(node)
    return matches


def _references_runtime(node: ast.AST) -> bool:
    """True when ``node``'s subtree references a runtime value rather than
    being a pure literal / literal collection — i.e. it contains a ``Name``,
    ``Attribute``, ``Call``, ``Subscript``, awaited, or starred expression.
    """
    for child in ast.walk(node):
        if isinstance(
            child,
            (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Await, ast.Starred),
        ):
            return True
    return False


def _is_vacuous_compare(node: ast.Compare) -> bool:
    """A comparison proves nothing about emitted state when it is either a
    structural self-tautology (every operand identical, e.g. ``x.foo == x.foo``
    or ``declared == declared``) or has no runtime reference at all
    (``"a" == "a"``)."""
    operands: list[ast.expr] = [node.left, *node.comparators]
    dumps = [ast.dump(op) for op in operands]
    if len(dumps) >= 2 and all(d == dumps[0] for d in dumps[1:]):
        return True
    return not any(_references_runtime(op) for op in operands)


def _vacuous_occurrence_ids(tree: ast.AST) -> set[int]:
    """Object ids of AST nodes whose literal occurrences do NOT constitute
    real coverage:

    * ``docstring`` / bare string-constant expression statements
      (``ast.Expr`` whose value is a string ``Constant``);
    * bare literal assertions (``assert "<state>"`` — an ``ast.Assert`` whose
      ``.test`` is a ``Constant``);
    * every node contained in a *vacuous* comparison (self-tautology or
      constant-vs-constant), so ``x.foo == x.foo`` and ``"a" == "a"`` do not
      count via either the attribute or the literal.

    Comments are absent from the AST entirely, so comment-only mentions are
    excluded for free.
    """
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            excluded.add(id(node.value))
        elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
            excluded.add(id(node.test))
        elif isinstance(node, ast.Compare) and _is_vacuous_compare(node):
            for child in ast.walk(node):
                excluded.add(id(child))
    return excluded


def _state_covered(state: str, test_contents: str | list[str]) -> bool:
    """A declared state is covered when its literal is genuinely referenced in
    a node's tests.

    Coverage is decided at the AST level, not by substring/regex presence. The
    state name — as a string constant, or (for identifier-shaped states) an
    ``.attr`` access (the enum idiom ``EnumX.STATE``) — must appear in a
    position that actually exercises behaviour. It is NOT counted when its only
    occurrences are vacuous: a bare ``assert "<state>"`` with no comparison, a
    self-tautology / constant-only comparison (``x == x``, ``"a" == "a"``), or a
    docstring / comment mention. Every other occurrence (compare operand, set /
    tuple / for-loop iterable feeding an assert, ``assertEqual`` argument, dict
    value, …) counts, matching the prior regex's reach without its blind spots.
    """
    if not state:
        return False
    sources = [test_contents] if isinstance(test_contents, str) else list(test_contents)
    identifier_state = bool(_IDENTIFIER_RE.match(state)) and "." not in state
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        excluded = _vacuous_occurrence_ids(tree)
        matches = _state_match_nodes(tree, state, identifier_state=identifier_state)
        if any(id(match) not in excluded for match in matches):
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

    test_sources = _node_test_sources(node_name, test_corpus)

    raw_uncovered = [s for s in ds.states if not _state_covered(s, test_sources)]

    # OMN-14151: a lifecycle-exempt node's baselined gaps never strict-promote
    # — see LIFECYCLE_EXEMPTIONS above.
    lifecycle_exempt = _contract_lifecycle(contract) in LIFECYCLE_EXEMPTIONS

    uncovered: list[str] = []
    baselined_uncovered: list[str] = []
    for state in raw_uncovered:
        is_baselined = (node_name, state) in baseline
        # Baselined pre-existing gaps stay WARN unless strict mode promotes
        # them — strict is only True here for nodes the caller marked
        # strict-eligible (directly modified in the diff), so untouched
        # legacy debt keeps its grandfather clause.
        if is_baselined and (not strict or lifecycle_exempt):
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


def _get_changed_nodes(git_ref: str) -> tuple[list[Path], set[str], set[str]]:
    """Return (node directories to validate, directly-modified node names,
    contract-touched node names).

    ``contract_touched`` (OMN-14009) is the subset of ``directly_modified``
    whose OWN ``contract.yaml`` appears in the diff — as opposed to being
    flagged only via a test-file association. Callers use this to scope the
    declared-state-shape comparison (see ``_resolve_strict_eligible``) to the
    exact collision this ticket fixes: the ``missing_handler_routing`` ratchet
    fixes a node by adding a purely-additive nested ``handler:`` sibling under
    ``handler_routing`` in that same ``contract.yaml`` — a change with nothing
    to do with the node's declared states, but which used to un-grandfather
    that node's unrelated, pre-existing baselined state-coverage debt purely
    because both ratchets share a file.
    """
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
    contract_touched: set[str] = set()
    for f in changed_files:
        parts = Path(f).parts
        if (
            len(parts) >= 4
            and parts[0] == "src"
            and parts[1] == "omnimarket"
            and parts[2] == "nodes"
            and parts[3].startswith("node_")
            # OMN-15376: a node's vendored DDL is not the node. Files under
            # <node>/migrations/*.sql cannot add, remove or rename a declared
            # FSM state or a declared output topic -- those come only from
            # contract.yaml -- so a migration-only edit must not un-grandfather
            # that node's pre-existing baselined state-coverage debt. Same
            # scoping principle as the OMN-14009 contract_touched carve-out
            # below: strictness follows the artifact that can actually move the
            # declared state shape. A 46-file shape-drift reconciliation across
            # the node migration corpus otherwise turns 16 unrelated nodes red.
            and not (len(parts) >= 5 and parts[4] == "migrations")
        ):
            directly_modified.add(parts[3])
            if len(parts) == 5 and parts[4] == "contract.yaml":
                contract_touched.add(parts[3])
        # A node's own tests being touched should also re-check its coverage.
        # Match on exact path segments (directory components / filename), NOT a
        # raw substring: a substring check spuriously flags a node whose name is
        # a prefix of a longer node's name (e.g. ``node_projection_delegation``
        # matching ``tests/integration/node_projection_delegation_inference_response/``),
        # which in strict mode wrongly promotes the shorter node's baselined
        # WARN to a FAIL even though it was never touched.
        if len(parts) >= 1 and parts[0] == "tests" and NODES_DIR.exists():
            path_segments = set(Path(f).as_posix().split("/"))
            for candidate in NODES_DIR.iterdir():
                if candidate.is_dir() and candidate.name in path_segments:
                    directly_modified.add(candidate.name)

    nodes: list[Path] = []
    for name in sorted(directly_modified):
        node_dir = NODES_DIR / name
        if node_dir.is_dir():
            nodes.append(node_dir)
    return nodes, directly_modified, contract_touched


def _read_contract_at_ref(rel_path: str, git_ref: str) -> dict[str, Any] | None:
    """Best-effort read of a contract.yaml's content at ``git_ref``.

    Returns ``None`` when the ref/path is unreadable (new file, shallow
    clone, detached/unpushed branch) so callers fail safe (treat as "shape
    genuinely changed") rather than silently exempting a brand-new node.
    """
    proc = subprocess.run(
        ["git", "show", f"{git_ref}:{rel_path}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        return None
    try:
        data = yaml.safe_load(proc.stdout) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _declared_state_shape_changed(node_name: str, git_ref: str) -> bool:
    """True when a node's state-coverage-relevant contract shape actually
    differs between ``git_ref`` and the working tree (OMN-14009).

    Deliberately compares the *extracted* :func:`declared_states` shape, not
    the raw file text — a handler_routing-only edit changes the file but
    leaves ``fsm.states`` / ``state_machine.states`` / ``outputs`` /
    ``event_bus.publish_topics`` / ``terminal_event`` untouched, and must not
    register as a shape change. Any genuine change to those keys, or an
    unreadable/missing ref (bootstrap, shallow clone), fails safe toward
    ``True`` (stays strict-eligible) rather than silently exempting real
    debt.
    """
    node_dir = NODES_DIR / node_name
    contract_path = node_dir / "contract.yaml"
    if not contract_path.is_file():
        return True
    try:
        current = yaml.safe_load(contract_path.read_text()) or {}
    except yaml.YAMLError:
        return True
    if not isinstance(current, dict):
        return True

    rel_path = f"src/omnimarket/nodes/{node_name}/contract.yaml"
    before = _read_contract_at_ref(rel_path, git_ref)
    if before is None:
        return True

    current_ds = declared_states(current)
    before_ds = declared_states(before)
    return (current_ds.kind, current_ds.states) != (before_ds.kind, before_ds.states)


def _resolve_strict_eligible(
    directly_modified: set[str], contract_touched: set[str], git_ref: str
) -> set[str]:
    """Narrow ``directly_modified`` to nodes genuinely eligible for strict-mode
    baseline promotion (OMN-14009).

    A node touched ONLY via its own ``contract.yaml`` (``contract_touched``),
    where the diff left the state-coverage-relevant shape unchanged, is exempt
    — this is exactly the ``missing_handler_routing`` ratchet's purely-additive
    handler_routing fix, which has nothing to do with the node's declared
    states. A node flagged via any other path (its own test files, handler
    code, etc.) keeps its existing strict eligibility unchanged; a node whose
    contract.yaml diff DID change the declared-state shape also stays
    eligible.
    """
    eligible: set[str] = set()
    for name in directly_modified:
        if name in contract_touched and not _declared_state_shape_changed(
            name, git_ref
        ):
            continue
        eligible.add(name)
    return eligible


def collect_nodes(*, changed_ref: str | None) -> tuple[list[Path], set[str] | None]:
    if changed_ref is not None:
        nodes, directly_modified, contract_touched = _get_changed_nodes(changed_ref)
        strict_eligible = _resolve_strict_eligible(
            directly_modified, contract_touched, changed_ref
        )
        return nodes, strict_eligible
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
