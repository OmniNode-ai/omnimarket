# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The graph itself: a pure function of repository snapshots and the overlay's root rules.

Entities are Python module files. An edge is an import (``import``, ``from ... import``, a relative
import, or a literal ``importlib.import_module`` / ``__import__`` name). An import under
``if TYPE_CHECKING`` loads nothing; it marks a lazy export instead, so ``from package import
name`` reaches the module the package names for it. Roots are ``__main__``
modules, files matching a root glob, the targets of ``pyproject.toml`` scripts and entry points,
and the ``-m`` modules and ``.py`` paths a command file runs (workflows, hook configs, units). A
contract links its node package to the modules it names, so a declared node's handler is reached
only when its package is (registered by an entry point, or imported). Reachability runs from the
roots over edges of
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
from datetime import date
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
    ModelWiringAllowance,
)

Entity = tuple[str, str]
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_PYPROJECT = "pyproject.toml"
_IMPORT_CALLS = frozenset({"import_module", "__import__"})
_MODULE_FLAG = re.compile(
    r"(?:^|\s)-m\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
_PY_PATH = re.compile(r"(?:^|[\s\"'=:(])((?:\./)?[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)\b")


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
    """The overlay's root rules, with the drill switch that drops the contract links."""

    roots: ModelGraphRoots
    contract_links: bool = True

    def wanted(self, path: str) -> bool:
        return (
            path.endswith(".py")
            or path == _PYPROJECT
            or _matches(path, self.roots.contract_globs)
            or _matches(path, self.roots.command_globs)
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


@dataclass
class _Scan:
    """What one module imports at run time, and what it names only for the type checker."""

    #: Dotted names imported at run time (``import x.y``, a literal ``import_module``).
    names: list[str] = field(default_factory=list)
    #: ``(base, name)`` of each run-time ``from base import name``.
    pairs: list[tuple[str, str]] = field(default_factory=list)
    #: Name to dotted module of each ``from m import name`` under ``if TYPE_CHECKING``: a lazy
    #: package exports these, so ``from package import name`` loads ``m`` on first use.
    typing_exports: dict[str, str] = field(default_factory=dict)


_NO_SCAN = _Scan()


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _from_base(node: ast.ImportFrom, package: Sequence[str]) -> str:
    if not node.level:
        return node.module or ""
    keep = len(package) - (node.level - 1)
    if keep < 0:
        return ""
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


def _scan(tree: ast.AST, package: Sequence[str]) -> _Scan:
    scan = _Scan()
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, typing_only = stack.pop()
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend((child, True) for child in node.body)
            stack.extend((child, typing_only) for child in node.orelse)
            continue
        if isinstance(node, ast.Import):
            if not typing_only:
                scan.names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base:
                for alias in node.names:
                    if alias.name == "*":
                        if not typing_only:
                            scan.names.append(base)
                    elif typing_only:
                        scan.typing_exports[alias.asname or alias.name] = (
                            f"{base}.{alias.name}"
                        )
                    else:
                        scan.pairs.append((base, alias.name))
        elif (
            not typing_only
            and isinstance(node, ast.Call)
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
            scan.names.append(node.args[0].value)
        stack.extend((child, typing_only) for child in ast.iter_child_nodes(node))
    return scan


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


def _command_modules(text: str) -> Iterator[str]:
    """``-m <module>`` in a command file (a workflow, a hook config, a unit, a shell script)."""
    for match in _MODULE_FLAG.finditer(text):
        yield match.group(1)


def _command_paths(text: str) -> Iterator[str]:
    """Repository-relative ``.py`` paths a command file runs."""
    for match in _PY_PATH.finditer(text):
        yield match.group(1).removeprefix("./")


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
    scans: dict[Entity, _Scan] = {}
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
                scans[entity] = _scan(tree, package)
            elif path == _PYPROJECT and rules.roots.entry_points:
                for module in _entry_point_modules(text):
                    roots.update(index.resolve(module, repo))
            elif rules.contract_links and _matches(path, rules.roots.contract_globs):
                # A contract links its node package to the modules it names: the runtime loads
                # them when it loads the node, so they are reached exactly when the package is.
                owner = (
                    repo,
                    f"{path.rsplit('/', 1)[0]}/__init__.py"
                    if "/" in path
                    else "__init__.py",
                )
                if owner in index.name_of:
                    links = edges.setdefault(owner, set())
                    for module in _contract_modules(
                        text, rules.roots.contract_module_keys
                    ):
                        links.update(index.resolve(module, repo))
            if _matches(path, rules.roots.command_globs):
                for module in _command_modules(text):
                    roots.update(index.resolve(module, repo))
                for named in _command_paths(text):
                    if (repo, named) in index.name_of:
                        roots.add((repo, named))

    for entity, scan in scans.items():
        repo = entity[0]
        targets = edges.setdefault(entity, set())
        for imported in scan.names:
            targets.update(index.resolve(imported, repo))
        for base, alias in scan.pairs:
            targets.update(index.resolve(f"{base}.{alias}", repo))
            for package_entity in index.exact(base, repo):
                exported = scans.get(package_entity, _NO_SCAN).typing_exports.get(alias)
                if exported is not None:
                    targets.update(index.resolve(exported, repo))

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
    base: ModelRepoSnapshot,
    head: ModelRepoSnapshot,
    rules: GraphRules,
    *,
    allowlist: Sequence[ModelWiringAllowance] = (),
    today: date | None = None,
) -> ModelGraphCheckResult:
    """Modules the head adds that no root reaches at the head (tests excluded).

    A live allowlist entry of the repository excuses the added paths it covers; an entry whose date
    has come excuses nothing and is itself a finding, so ``today`` is required with an allowlist.
    """
    entries = tuple(entry for entry in allowlist if entry.repo == head.repo)
    if entries and today is None:
        raise ValueError("an allowlist is judged on a day: pass today")
    graph = build_graph([head], rules)
    added = sorted(
        path
        for path in head.files
        if path.endswith(".py") and path not in base.files and not rules.is_test(path)
    )
    unreached = [path for path in added if (head.repo, path) not in graph.reached]
    live = tuple(
        entry for entry in entries if today is not None and not entry.expired_on(today)
    )
    allowed = tuple(
        path for path in unreached if any(entry.covers(path) for entry in live)
    )
    return ModelGraphCheckResult(
        repo=head.repo,
        base=base.head,
        head=head.head,
        added=len(added),
        flagged=tuple(path for path in unreached if path not in allowed),
        allowed=allowed,
        expired=tuple(
            f"{entry.path} expired {entry.expires.isoformat()}"
            for entry in entries
            if today is not None and entry.expired_on(today)
        ),
    )
