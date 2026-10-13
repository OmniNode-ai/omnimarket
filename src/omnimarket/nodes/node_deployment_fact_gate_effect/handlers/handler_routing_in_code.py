# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a new code path that chooses a route (OMN-20601).

Ruling 2026-10-10T17:54:30Z: routing decisions come from overlays, never from
omnimarket code. A code path that chooses a route compares a routing selector (a
task class, tier, backend, provider, rung, harness or model name) with a string
literal. This module counts those comparisons in the package's non-test code, in
the tree and at a base revision, and reports each one whose count in the tree is
above its count at the base revision.

The base revision is the baseline, as for the packaged configs: it only shrinks. A
comparison moved out of code is absent from every later base, so putting it back is
new again. A comparison is identified by its file, its enclosing scope and its
unparsed expression, so a reformat does not make it new; a rename of its file or
function does.

The scan reads the source of ``src/omnimarket`` and excludes ``tests`` directories,
``test_*.py`` and ``conftest*.py``. It sees a comparison of a selector name with a
string literal, a literal tuple, list or set. It does not see a mapping keyed by
task class or a call that takes a literal; the inventory test in
``tests/test_routing_sources_omn20601.py`` covers the configuration sources.
"""

from __future__ import annotations

import ast
import io
import subprocess
import tarfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Final

from omnimarket.nodes.node_deployment_fact_gate_effect.models.model_routing_in_code_gate import (
    ModelNewRoutingBranch,
)

#: Where the scanned code lives, relative to the repository root.
SCANNED_SOURCE_DIR: Final[str] = "src/omnimarket"

#: Identifiers that carry a routing choice.
ROUTING_SELECTORS: Final[frozenset[str]] = frozenset(
    {
        "task_class",
        "tier",
        "tier_id",
        "tier_name",
        "next_tier_name",
        "routing_tier",
        "backend",
        "backend_id",
        "provider",
        "route",
        "rung",
        "harness",
        "model_key",
        "model_id",
        "model_name",
    }
)

_GIT_TIMEOUT_S: Final[int] = 120


class RoutingInCodeGateError(RuntimeError):
    """The scan could not read the tree or the base revision."""


def _is_scanned(relative: PurePosixPath) -> bool:
    if relative.suffix != ".py" or "tests" in relative.parts:
        return False
    return not relative.name.startswith(("test_", "conftest"))


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_string_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_string_literal(e) for e in node.elts)
    return False


def _selects_route(node: ast.Compare) -> bool:
    operands = [node.left, *node.comparators]
    for index, op in enumerate(node.ops):
        left, right = operands[index], operands[index + 1]
        if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
            continue
        if _name_of(left) in ROUTING_SELECTORS and _is_string_literal(right):
            return True
        # `"literal" == selector`; `"literal" in selector` is a substring test.
        if (
            isinstance(op, (ast.Eq, ast.NotEq))
            and _name_of(right) in ROUTING_SELECTORS
            and _is_string_literal(left)
        ):
            return True
    return False


class _BranchCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: list[tuple[str, str]] = []

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_Compare(self, node: ast.Compare) -> None:
        if _selects_route(node):
            self.found.append((".".join(self.scope) or "<module>", ast.unparse(node)))
        self.generic_visit(node)


def routing_branches(sources: Iterable[tuple[str, str]]) -> Counter[str]:
    """Count the route-selecting comparisons of ``(repo-relative path, source text)`` pairs.

    Each key is ``<path>::<enclosing scope>::<expression>``.
    """
    counts: Counter[str] = Counter()
    for path, text in sources:
        if not _is_scanned(PurePosixPath(path)):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise RoutingInCodeGateError(f"{path} does not parse: {exc}") from exc
        collector = _BranchCollector()
        collector.visit(tree)
        for scope, expression in collector.found:
            counts[f"{path}::{scope}::{expression}"] += 1
    return counts


def judge_routing_branches(
    head: Counter[str], base: Counter[str]
) -> tuple[ModelNewRoutingBranch, ...]:
    """The comparisons whose count in the tree is above their count at the base. Pure."""
    return tuple(
        ModelNewRoutingBranch(
            fingerprint=key, base_count=base[key], head_count=head[key]
        )
        for key in sorted(head)
        if head[key] > base[key]
    )


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    done = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise RoutingInCodeGateError(
            f"git {' '.join(args)} failed in {repo_root}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()}"
        )
    return done.stdout


def read_head_sources(repo_root: Path) -> list[tuple[str, str]]:
    """Every scanned file of the tree, as ``(repo-relative path, text)``."""
    root = repo_root / SCANNED_SOURCE_DIR
    if not root.is_dir():
        raise RoutingInCodeGateError(f"{SCANNED_SOURCE_DIR} is missing in {repo_root}")
    return [
        (path.relative_to(repo_root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
        if _is_scanned(PurePosixPath(path.relative_to(repo_root).as_posix()))
    ]


def read_base_sources(repo_root: Path, base_ref: str) -> list[tuple[str, str]]:
    """Every scanned file at ``base_ref``, read through ``git archive``."""
    _git_bytes(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    archive = _git_bytes(
        repo_root, "archive", "--format=tar", base_ref, "--", SCANNED_SOURCE_DIR
    )
    sources: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not _is_scanned(PurePosixPath(member.name)):
                continue
            extracted = tar.extractfile(member)
            if extracted is not None:
                sources.append((member.name, extracted.read().decode("utf-8")))
    return sources


def judge_tree(
    repo_root: Path, base_ref: str | None
) -> tuple[tuple[ModelNewRoutingBranch, ...], tuple[str, ...]]:
    """The new route-selecting comparisons against ``base_ref`` and every one the tree carries."""
    head = routing_branches(read_head_sources(repo_root))
    base = (
        routing_branches(read_base_sources(repo_root, base_ref))
        if base_ref is not None
        else Counter[str]()
    )
    new = judge_routing_branches(head, base) if base_ref is not None else ()
    return new, tuple(sorted(head))


__all__: list[str] = [
    "ROUTING_SELECTORS",
    "SCANNED_SOURCE_DIR",
    "RoutingInCodeGateError",
    "judge_routing_branches",
    "judge_tree",
    "read_base_sources",
    "read_head_sources",
    "routing_branches",
]
