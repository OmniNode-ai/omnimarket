# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The graph itself: a pure function of repository snapshots and the overlay's root rules.

Entities are Python module files. An edge is an import (``import``, ``from ... import``, a relative
import, or a literal ``importlib.import_module`` / ``__import__`` name). Roots are ``__main__``
modules, files matching a root glob, the modules a contract names, and the targets of
``pyproject.toml`` scripts and entry points. Reachability runs from the roots over edges of
non-test files only, across every repository in the run, so one repository's import of another
counts. Test files are read, are never roots, and reach nothing: a module only a test imports is
unreachable and is named as such. It reads no clock, file, network or environment.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from omnimarket.models.model_code_graph import (
    ModelGraphCheckResult,
    ModelRepoGraph,
    ModelRepoSnapshot,
    ModelSampleOutcome,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelGraphRoots,
    ModelGraphSample,
)

Entity = tuple[str, str]
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_PYPROJECT = "pyproject.toml"
_IMPORT_CALLS = frozenset({"import_module", "__import__"})


@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """A path glob where ``**/`` spans directories, ``*`` stays inside one, ``?`` is one char."""
    out = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif pattern[index] == "*":
            out += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(pattern[index])
            index += 1
    return re.compile(f"^{out}$")


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_regex(pattern).match(path) for pattern in patterns)


@dataclass(frozen=True)
class GraphRules:
    """The overlay's root rules, with the drill switch that drops the contract roots."""

    roots: ModelGraphRoots
    contract_roots: bool = True

    def wanted(self, path: str) -> bool:
        return (
            path.endswith(".py")
            or path == _PYPROJECT
            or _matches(path, self.roots.contract_globs)
        )

    def is_test(self, path: str) -> bool:
        return _matches(path, self.roots.test_globs)

    def module_name(self, path: str) -> str:
        relative = path
        for directory in self.roots.source_dirs:
            prefix = directory.strip("/") + "/"
            if path.startswith(prefix):
                relative = path[len(prefix) :]
                break
        name = relative[: -len(".py")].replace("/", ".")
        return name[: -len(".__init__")] if name.endswith(".__init__") else name


@dataclass
class _Index:
    by_name: dict[str, list[Entity]] = field(default_factory=dict)
    name_of: dict[Entity, str] = field(default_factory=dict)

    def add(self, entity: Entity, name: str) -> None:
        self.by_name.setdefault(name, []).append(entity)
        self.name_of[entity] = name

    def exact(self, name: str, repo: str) -> list[Entity]:
        found = self.by_name.get(name, [])
        own = [entity for entity in found if entity[0] == repo]
        return own or found

    def resolve(self, name: str, repo: str) -> list[Entity]:
        """The module a dotted name lands on (its longest known prefix) and its packages."""
        parts = name.split(".")
        hit: list[Entity] = []
        for cut in range(len(parts), 0, -1):
            hit = self.exact(".".join(parts[:cut]), repo)
            if hit:
                break
        else:
            return []
        packages = [
            entity
            for size in range(1, cut)
            for entity in self.exact(".".join(parts[:size]), repo)
        ]
        return hit + packages


def _imported_names(tree: ast.AST, package: Sequence[str]) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base = ".".join(
                    [*package[:keep], *([node.module] if node.module else [])]
                )
            else:
                base = node.module or ""
            if not base:
                continue
            yield base
            for alias in node.names:
                if alias.name != "*":
                    yield f"{base}.{alias.name}"
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _IMPORT_CALLS
                )
                or (isinstance(node.func, ast.Name) and node.func.id in _IMPORT_CALLS)
            )
        ):
            yield node.args[0].value


def _contract_modules(text: str, keys: Sequence[str]) -> Iterator[str]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    stack: list[object] = [document]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if (
                    key in keys
                    and isinstance(value, str)
                    and _DOTTED.match(value.strip())
                ):
                    yield value.strip()
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def _entry_point_modules(text: str) -> Iterator[str]:
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError:
        return
    groups: list[object] = [project.get("scripts", {}), project.get("gui-scripts", {})]
    groups.extend((project.get("entry-points") or {}).values())
    for group in groups:
        if isinstance(group, dict):
            for target in group.values():
                if isinstance(target, str):
                    yield target.split(":", 1)[0].strip()


@dataclass(frozen=True)
class Graph:
    """Every module, the test ones, the roots, the edges and what the roots reach."""

    modules: frozenset[Entity]
    tests: frozenset[Entity]
    roots: frozenset[Entity]
    test_imports: frozenset[Entity]
    reached: frozenset[Entity]
    heads: tuple[tuple[str, str], ...]

    def repo_graph(self, repo: str, head: str) -> ModelRepoGraph:
        mine = {entity for entity in self.modules if entity[0] == repo}
        tests = {entity for entity in self.tests if entity[0] == repo}
        unreachable = sorted(entity[1] for entity in mine - tests - self.reached)
        return ModelRepoGraph(
            repo=repo,
            head=head,
            modules=len(mine),
            test_modules=len(tests),
            roots=sum(1 for entity in self.roots if entity[0] == repo),
            reachable=len(mine & self.reached),
            unreachable=tuple(unreachable),
            test_only=tuple(
                path for path in unreachable if (repo, path) in self.test_imports
            ),
        )


def build_graph(snapshots: Sequence[ModelRepoSnapshot], rules: GraphRules) -> Graph:
    index = _Index()
    tests: set[Entity] = set()
    for snapshot in snapshots:
        for path in snapshot.files:
            if path.endswith(".py"):
                entity = (snapshot.repo, path)
                index.add(entity, rules.module_name(path))
                if rules.is_test(path):
                    tests.add(entity)

    edges: dict[Entity, set[Entity]] = {}
    roots: set[Entity] = set()
    for snapshot in snapshots:
        repo = snapshot.repo
        for path, text in snapshot.files.items():
            entity = (repo, path)
            if path.endswith(".py"):
                if entity not in tests and (
                    (
                        rules.roots.main_modules
                        and path.rsplit("/", 1)[-1] == "__main__.py"
                    )
                    or _matches(path, rules.roots.root_globs)
                ):
                    roots.add(entity)
                try:
                    tree = ast.parse(text)
                except (SyntaxError, ValueError):
                    continue
                name = index.name_of[entity]
                package = (
                    name.split(".")
                    if path.endswith("__init__.py")
                    else name.split(".")[:-1]
                )
                targets = edges.setdefault(entity, set())
                for imported in _imported_names(tree, package):
                    targets.update(index.resolve(imported, repo))
            elif path == _PYPROJECT and rules.roots.entry_points:
                for module in _entry_point_modules(text):
                    roots.update(index.resolve(module, repo))
            elif rules.contract_roots and _matches(path, rules.roots.contract_globs):
                for module in _contract_modules(text, rules.roots.contract_module_keys):
                    roots.update(index.resolve(module, repo))

    reached: set[Entity] = set()
    queue = deque(roots - tests)
    while queue:
        entity = queue.popleft()
        if entity in reached or entity in tests:
            continue
        reached.add(entity)
        queue.extend(edges.get(entity, ()))
    test_imports = {target for test in tests for target in edges.get(test, ())}
    return Graph(
        modules=frozenset(index.name_of),
        tests=frozenset(tests),
        roots=frozenset(roots),
        test_imports=frozenset(test_imports),
        reached=frozenset(reached),
        heads=tuple((snapshot.repo, snapshot.head) for snapshot in snapshots),
    )


def judge_sample(graph: Graph, sample: ModelGraphSample) -> ModelSampleOutcome:
    """How many sample entries the graph agrees with, and whether every control is reached."""
    misses: list[str] = []
    agree = 0
    for entry in sample.entries:
        entity = (entry.repo, entry.path)
        if entity not in graph.modules:
            misses.append(f"sample-missing:{entry.repo}/{entry.path}")
            continue
        used = entity in graph.reached
        if used == (entry.expected == "used"):
            agree += 1
        else:
            misses.append(
                f"sample-disagrees:{entry.repo}/{entry.path}:expected-{entry.expected}"
            )
    reached = 0
    for control in sample.controls:
        entity = (control.repo, control.path)
        if entity in graph.reached:
            reached += 1
        elif entity not in graph.modules:
            misses.append(f"control-missing:{control.repo}/{control.path}")
        else:
            misses.append(f"control-unreached:{control.repo}/{control.path}")
    return ModelSampleOutcome(
        agree=agree,
        total=len(sample.entries),
        controls_reached=reached,
        controls_total=len(sample.controls),
        misses=tuple(misses),
    )


def check_added(
    base: ModelRepoSnapshot, head: ModelRepoSnapshot, rules: GraphRules
) -> ModelGraphCheckResult:
    """Modules the head adds that no root reaches at the head (tests excluded)."""
    graph = build_graph([head], rules)
    added = sorted(
        path
        for path in head.files
        if path.endswith(".py") and path not in base.files and not rules.is_test(path)
    )
    flagged = tuple(path for path in added if (head.repo, path) not in graph.reached)
    return ModelGraphCheckResult(
        repo=head.repo,
        base=base.head,
        head=head.head,
        added=len(added),
        flagged=flagged,
    )
