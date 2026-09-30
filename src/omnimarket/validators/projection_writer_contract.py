# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A projection writer reports its count under the key the runtime reads.

What this is for
----------------
The runtime counts a projection write in exactly one place,
``omnibase_infra`` ``handler_wiring._extract_rows_upserted``. It reads the key
``rows_upserted`` and reads anything else as zero. A writer that reported
``rows_written`` therefore wrote its rows and was counted as having written
none: the write-path guard logged an ERROR for every message, no terminal event
was emitted, and the ``projection_apply_divergence`` health dimension turned the
runtime DEGRADED while the table filled correctly (OMN-19833, measured on the
dev lane 2026-09-30, first fixed for ``node_projection_pr_landing`` in
omnimarket#3098). ``node_projection_ci_attempt_outcome`` and
``node_projection_worktree_reconcile`` carried the same defect.

The same fix found a second defect of the same family. A node that routes an
in-process writer AND a pure fold in ``handler_routing`` has both dispatched on
the writer's topics. The fold is handed the raw event, not the wrapped request
it validates, so it fails (or runs on defaults) beside the writer, and it can
never report a write. The writer calls the fold in process (rule 7a); the
contract routes the writer only.

Two refusals
------------
1. **Wrong count key.** A module that defines an in-process writer and never
   reports ``rows_upserted`` as a mapping key or keyword argument. Extra
   ``rows_*`` statistics beside it are harmless to the runtime and allowed.
2. **A routed pure fold beside an in-process writer.** Any ``handler_routing``
   entry, in a contract that routes an in-process writer, whose class is not
   that writer.

A node whose only runner is standalone (it does not declare
``onex_runtime_inprocess_dispatch``) is out of scope: there the typed fold is the
in-process entry, which is the sanctioned dedicated-writer shape.

Shape
-----
Pure syntax-tree and YAML reads over a nodes root, no import of the handlers, so
a synthetic tree in ``tmp_path`` is proven the same way the real one is. A
vacuity floor fails the scan when it finds far fewer writers than the tree
holds, because a scan that saw nothing is a broken scan and not a clean tree.

Exit codes: 0 clean, 1 on any refusal or on the floor.

Related Tickets:
    - OMN-19833: the PR landing projection fix (omnimarket#3098) and this gate
    - OMN-18769: the two-class projection shape (rule 7a)
    - OMN-18992: the runtime's guard-refusal key
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

#: The one key the runtime counts a write under.
ROWS_UPSERTED_KEY = "rows_upserted"

#: The class attribute that opts a runner-shaped handler in to in-process
#: dispatch by the shared runtime.
INPROCESS_ATTR = "onex_runtime_inprocess_dispatch"

BASE_RUNNER_NAME = "BaseProjectionRunner"

#: Fewer writers than this means the scan is broken, not that the tree is clean.
#: The tree holds fifteen today.
MIN_WRITERS = 12

_MODULE_PREFIX = "omnimarket.nodes."


@dataclass(frozen=True)
class Finding:
    """One refusal, addressed to the node and the thing that broke the rule."""

    node: str
    subject: str
    message: str

    def render(self) -> str:
        return f"{self.node}: {self.subject}: {self.message}"


def _module_file(nodes_root: Path, dotted: str) -> Path | None:
    if not dotted.startswith(_MODULE_PREFIX):
        return None
    return nodes_root.joinpath(*dotted[len(_MODULE_PREFIX) :].split(".")).with_suffix(
        ".py"
    )


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _base_names(cls: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _declares_inprocess_dispatch(cls: ast.ClassDef) -> bool:
    for stmt in cls.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id == INPROCESS_ATTR
                and isinstance(value, ast.Constant)
                and value.value is True
            ):
                return True
    return False


def is_inprocess_writer(cls: ast.ClassDef) -> bool:
    """A projection runner the shared runtime dispatches in process."""
    return BASE_RUNNER_NAME in _base_names(cls) and _declares_inprocess_dispatch(cls)


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a named key resolves."""
    resolved: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            resolved[target.id] = value.value
    return resolved


def reported_row_keys(tree: ast.Module) -> set[str]:
    """Every ``rows_*`` key a module puts in a mapping or a keyword argument."""
    constants = _string_constants(tree)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                resolved = _resolve_key(key, constants)
                if resolved is not None and resolved.startswith("rows_"):
                    keys.add(resolved)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is not None and keyword.arg.startswith("rows_"):
                    keys.add(keyword.arg)
        elif isinstance(node, ast.Subscript):
            resolved = _resolve_key(node.slice, constants)
            if resolved is not None and resolved.startswith("rows_"):
                keys.add(resolved)
    return keys


def _resolve_key(node: ast.expr | None, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _routed_entries(contract: dict[str, object]) -> list[tuple[str, str]]:
    routing = contract.get("handler_routing")
    if not isinstance(routing, dict):
        return []
    entries: list[tuple[str, str]] = []
    for item in routing.get("handlers") or []:
        handler = item.get("handler") if isinstance(item, dict) else None
        if isinstance(handler, dict) and handler.get("name") and handler.get("module"):
            entries.append((str(handler["name"]), str(handler["module"])))
    return entries


def scan_node(nodes_root: Path, node_dir: Path) -> tuple[list[Finding], int]:
    """Refusals for one node, and how many in-process writers it routes."""
    contract_path = node_dir / "contract.yaml"
    if not contract_path.is_file():
        return [], 0
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(contract, dict):
        return [], 0

    node = node_dir.name
    trees: dict[str, ast.Module] = {}
    writers: set[tuple[str, str]] = set()
    entries = _routed_entries(contract)
    for name, module in entries:
        path = _module_file(nodes_root, module)
        if path is None or not path.is_file():
            continue
        if module not in trees:
            trees[module] = ast.parse(path.read_text(encoding="utf-8"))
        cls = _find_class(trees[module], name)
        if cls is not None and is_inprocess_writer(cls):
            writers.add((name, module))

    findings: list[Finding] = []
    if not writers:
        return findings, 0

    for name, module in sorted(writers):
        keys = reported_row_keys(trees[module])
        if ROWS_UPSERTED_KEY not in keys:
            others = ", ".join(repr(k) for k in sorted(keys)) or "no rows_* key"
            findings.append(
                Finding(
                    node,
                    name,
                    f"never reports {ROWS_UPSERTED_KEY!r} (found {others}); the "
                    "runtime counts a write only under that key and reads any "
                    "other as zero rows, so every message it writes is counted "
                    "as none",
                )
            )

    for name, module in entries:
        if (name, module) not in writers:
            findings.append(
                Finding(
                    node,
                    name,
                    "is routed in handler_routing beside an in-process writer; the "
                    "runtime hands it the raw event beside the writer, so it can "
                    "never report a write. The writer calls the fold in process "
                    "(rule 7a), so route the writer only",
                )
            )
    return findings, len(writers)


def scan(nodes_root: Path) -> tuple[list[Finding], int]:
    """Refusals over every node under *nodes_root*, and the writers seen."""
    findings: list[Finding] = []
    writers = 0
    for node_dir in sorted(p for p in nodes_root.iterdir() if p.is_dir()):
        node_findings, node_writers = scan_node(nodes_root, node_dir)
        findings.extend(node_findings)
        writers += node_writers
    return findings, writers


def default_nodes_root() -> Path:
    return Path(__file__).resolve().parent.parent / "nodes"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes-root", type=Path, default=default_nodes_root())
    args = parser.parse_args(argv)

    findings, writers = scan(args.nodes_root)
    if writers < MIN_WRITERS:
        sys.stderr.write(
            f"projection-writer-contract: saw {writers} in-process writers, below "
            f"the floor of {MIN_WRITERS}; the scan is broken, not the tree clean\n"
        )
        return 1
    for finding in findings:
        sys.stderr.write(finding.render() + "\n")
    if findings:
        sys.stderr.write(f"projection-writer-contract: {len(findings)} refusal(s)\n")
        return 1
    sys.stdout.write(
        f"projection-writer-contract: {writers} in-process writers, clean\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
