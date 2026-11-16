# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Maps a change's files to the test paths it can affect (OMN-23719).

scripts/ci/detect_test_paths.py used to hold this logic. The canonical-file-shape ratchet (OMN-20304)
lets a baselined script only shrink, so the mapping lives here, in the selector node, and the script
calls it. A module listed in the adjacency map's ``node_scoped_modules`` is narrowed by the import
graph: the changed nodes' tests, the tests of every node that imports them (transitively), the tests
of nodes whose YAML names them, and the tests that import or name them. A change the handler cannot
attribute falls back to the whole ``tests/<module>/`` directory.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
    ModelPathResolutionRequest,
    ModelPathResolutionResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_source_text import (
    ProtocolSourceText,
)

_DEFAULT_SOURCE = SourceTextFile()
SRC_PREFIX = "src/omnimarket/"
TEST_PREFIX = "tests/"

PACKAGE = "omnimarket"
NODE_PREFIX = "node_"
_DOTTED_REFERENCE = re.compile(r"\b(?:omnimarket|tests)(?:\.[A-Za-z_]\w*)+")
_NODE_NAME = re.compile(r"\bnode_[a-z0-9_]+\b")
_TEST_FILE_PATH = re.compile(r"^tests/[\w/]+\.py$")


def _is_test_file(path: str) -> bool:
    """pytest's default python_files: test_*.py and *_test.py."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _dotted(path: str) -> str:
    """Importable name of a repo-relative .py path (src/ and . are on pythonpath)."""
    parts = path[: -len(".py")].split("/")
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _references(text: str, module: str, is_package: bool) -> set[str]:
    """Dotted names a file imports or spells in a string (importlib, patch targets).

    Parsed with ast so ``from omnimarket.handlers import adapter_x`` and relative
    imports resolve; a file that does not parse falls back to the dotted names its
    text spells, which still covers every absolute import.
    """
    found = set(_DOTTED_REFERENCE.findall(text))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return found
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(package):
                    continue
                base = package[: len(package) - (node.level - 1)]
                target = ".".join([*base, *([node.module] if node.module else [])])
            else:
                target = node.module or ""
            if target:
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


@dataclass(frozen=True)
class _ImportGraph:
    """Which files depend on which, over src/omnimarket/ and tests/ (OMN-23719).

    A vertex is a dotted module name, except that every file of one node package
    (``src/omnimarket/<module>/node_*/``) is the one vertex ``<module>/<node>``.
    """

    modules: frozenset[str]
    """Dotted name of every file in the graph."""
    paths: dict[str, str]
    """vertex -> repo-relative path, for test-file vertices only."""
    dependents: dict[str, frozenset[str]]
    """vertex -> vertices whose files import it."""
    yaml_namers: dict[str, frozenset[str]]
    """node vertex -> node vertices whose YAML names that node."""
    test_namers: dict[str, frozenset[str]]
    """node directory name -> test files that spell it."""

    def resolve(self, name: str) -> str | None:
        """Longest dotted prefix of ``name`` that is a file in the graph."""
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in self.modules:
                return candidate
            parts.pop()
        return None


def _vertex(dotted: str, node_scoped: frozenset[str]) -> str:
    parts = dotted.split(".")
    if (
        len(parts) >= 3
        and parts[0] == PACKAGE
        and parts[1] in node_scoped
        and parts[2].startswith(NODE_PREFIX)
    ):
        return f"{parts[1]}/{parts[2]}"
    return dotted


@cache
def _import_graph(
    repo_root: Path, node_scoped: frozenset[str], source: ProtocolSourceText
) -> _ImportGraph:
    files: dict[str, Path] = {}
    packages: set[str] = set()
    for base in (repo_root / "src" / PACKAGE, repo_root / "tests"):
        for path in base.rglob("*.py"):
            rel = path.relative_to(repo_root).as_posix()
            dotted = _dotted(rel)
            files[dotted] = path
            if path.name == "__init__.py":
                packages.add(dotted)

    lookup = _ImportGraph(frozenset(files), {}, {}, {}, {})
    dependents: defaultdict[str, set[str]] = defaultdict(set)
    paths: dict[str, str] = {}
    test_namers: defaultdict[str, set[str]] = defaultdict(set)
    for dotted, path in files.items():
        vertex = _vertex(dotted, node_scoped)
        text = source.read_text(path)
        if dotted.split(".")[0] == "tests":
            rel = path.relative_to(repo_root).as_posix()
            paths[vertex] = rel
            for name in set(_NODE_NAME.findall(text)):
                test_namers[name].add(rel)
        for name in _references(text, dotted, dotted in packages):
            target = lookup.resolve(name)
            if target is None:
                continue
            target_vertex = _vertex(target, node_scoped)
            if target_vertex != vertex:
                dependents[target_vertex].add(vertex)

    yaml_namers: defaultdict[str, set[str]] = defaultdict(set)
    for module in node_scoped:
        module_root = repo_root / "src" / PACKAGE / module
        if not module_root.is_dir():
            continue
        nodes = {
            child.name
            for child in module_root.iterdir()
            if child.is_dir() and child.name.startswith(NODE_PREFIX)
        }
        for node in nodes:
            for path in (module_root / node).rglob("*.y*ml"):
                text = source.read_text(path)
                for name in set(_NODE_NAME.findall(text)):
                    if name != node and name in nodes:
                        yaml_namers[f"{module}/{name}"].add(f"{module}/{node}")

    return _ImportGraph(
        modules=frozenset(files),
        paths=paths,
        dependents={k: frozenset(v) for k, v in dependents.items()},
        yaml_namers={k: frozenset(v) for k, v in yaml_namers.items()},
        test_namers={k: frozenset(v) for k, v in test_namers.items()},
    )


def _package_seeds(
    graph: _ImportGraph, seed: str, path: str, node_scoped: frozenset[str]
) -> set[str]:
    """The vertices a changed file seeds.

    A changed package ``__init__.py`` runs on every import of anything beneath
    it, so it seeds every module of that package, not only importers of the
    package name itself.
    """
    if not path.endswith("/__init__.py") or _dotted(path) != seed:
        return {_vertex(seed, node_scoped)}
    return {
        _vertex(name, node_scoped)
        for name in graph.modules
        if name == seed or name.startswith(f"{seed}.")
    }


def _node_scoped_selection(
    module: str,
    changed_files: list[str],
    node_scoped: frozenset[str],
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str] | None:
    """Test paths a change can affect inside node-scoped ``module`` (OMN-23719).

    None means the change cannot be attributed, and the caller selects the whole
    ``tests/<module>/`` directory instead.
    """
    graph = _import_graph(repo_root, node_scoped, source)
    test_root = f"{TEST_PREFIX}{module}/"
    seeds: set[str] = set()
    own_nodes: set[str] = set()
    selected: set[str] = set()
    for path in changed_files:
        if path.startswith(SRC_PREFIX):
            parts = path[len(SRC_PREFIX) :].split("/")
            if (
                len(parts) >= 3
                and parts[0] in node_scoped
                and parts[1].startswith(NODE_PREFIX)
            ):
                if not (repo_root / SRC_PREFIX / parts[0] / parts[1]).is_dir():
                    return None
                vertex = f"{parts[0]}/{parts[1]}"
                seeds.add(vertex)
                if parts[0] == module:
                    own_nodes.add(vertex)
            elif path.endswith(".py"):
                # A deleted module's importers now resolve to its nearest
                # surviving package, so that package seeds the walk.
                seed = graph.resolve(_dotted(path))
                if seed is None or seed == PACKAGE:
                    return None
                seeds.update(_package_seeds(graph, seed, path, node_scoped))
            else:
                return None
        elif path.startswith(test_root):
            parts = path[len(test_root) :].split("/")
            if len(parts) >= 2 and parts[0].startswith(NODE_PREFIX):
                selected.add(f"{test_root}{parts[0]}/")
                own_nodes.add(f"{module}/{parts[0]}")
            if path.endswith(".py"):
                seed = graph.resolve(_dotted(path))
                if seed is not None:
                    seeds.update(_package_seeds(graph, seed, path, node_scoped))

    affected = set(seeds)
    frontier = list(seeds)
    while frontier:
        for dependent in graph.dependents.get(frontier.pop(), ()):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    nodes = {v for v in affected if v.startswith(f"{module}/")}
    nodes |= {
        namer for node in list(nodes) for namer in graph.yaml_namers.get(node, ())
    }

    for node in own_nodes:
        name = node.split("/", 1)[1]
        has_tests = (repo_root / test_root / name).is_dir() or any(
            _is_test_file(graph.paths[v])
            for v in graph.dependents.get(node, ())
            if v in graph.paths
        )
        if not has_tests and not any(
            _is_test_file(p) for p in graph.test_namers.get(name, ())
        ):
            return None

    for node in nodes:
        name = node.split("/", 1)[1]
        if (repo_root / test_root / name).is_dir():
            selected.add(f"{test_root}{name}/")
        selected.update(p for p in graph.test_namers.get(name, ()) if _is_test_file(p))
    for vertex in affected:
        test_path = graph.paths.get(vertex)
        if test_path is None:
            continue
        if _is_test_file(test_path):
            selected.add(test_path)
        elif test_path.rsplit("/", 1)[-1] in ("conftest.py", "__init__.py"):
            # pytest loads these for every test beneath them without an import.
            selected.add(test_path.rsplit("/", 1)[0] + "/")
    selected.update(
        p.relative_to(repo_root).as_posix()
        for p in (repo_root / test_root).glob("*.py")
        if _is_test_file(p.name)
    )
    # TestPath admits a file only as tests/[\w/]+.py; any other file name is
    # selected through its directory rather than dropped.
    selected = {
        p if p.endswith("/") or _TEST_FILE_PATH.match(p) else p.rsplit("/", 1)[0] + "/"
        for p in selected
    }
    directories = tuple(p for p in selected if p.endswith("/"))
    return {p for p in selected if p.endswith("/") or not p.startswith(directories)}


def _resolve(
    changed_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
    source: ProtocolSourceText,
) -> list[str]:
    direct_modules: set[str] = set()
    selected: set[str] = set()
    node_scoped = frozenset(config.node_scoped_modules)
    scoped_test_modules: set[str] = set()

    for path in changed_files:
        if path.startswith(SRC_PREFIX):
            module = path[len(SRC_PREFIX) :].split("/", 1)[0]
            if module in config.adjacency:
                direct_modules.add(module)
        elif path.startswith(TEST_PREFIX):
            parts = path.split("/")
            if len(parts) == 2 and path.endswith(".py"):
                # OMN-15277: a root-level test file (no subdirectory) has no
                # containing directory below tests/ itself. Constructing
                # tests/<file>.py/ here would produce a pseudo-directory that
                # is either silently dropped by the on-disk filter below
                # (file absent at resolution time) or a file-shaped TestPath
                # value that only "works" via an accidental pathlib
                # trailing-slash normalization (file present). This function
                # has no full-suite-escalation concept of its own (that lives
                # in compute_selection()'s CHANGED_TEST_UNNARROWABLE check,
                # which short-circuits before ever reaching this function) --
                # contribute the real containing scope, tests/ itself,
                # instead of a malformed pseudo-path. CodeRabbit-flagged gap:
                # this direct resolve_test_paths() API has no equivalent
                # escalation of its own, so it must never emit the malformed
                # shape either.
                selected.add(TEST_PREFIX)
            elif (
                len(parts) >= 4
                and parts[1] in node_scoped
                and parts[2].startswith(NODE_PREFIX)
            ):
                # OMN-23719: a change inside one node's test directory narrows
                # with the node-scoped selection below.
                scoped_test_modules.add(parts[1])
            elif len(parts) >= 2:
                selected.add(f"{TEST_PREFIX}{parts[1]}/")

    expanded: set[str] = set(direct_modules)
    for module in direct_modules:
        expanded.update(config.adjacency[module].reverse_deps)

    for module in expanded | scoped_test_modules:
        if module in node_scoped:
            narrowed = _node_scoped_selection(
                module, changed_files, node_scoped, repo_root, source
            )
            if narrowed is not None:
                selected.update(narrowed)
                continue
        selected.add(f"{TEST_PREFIX}{module}/")

    return sorted(p for p in selected if (repo_root / p).exists())


class HandlerTestPathResolution:
    """Pure over its injected text source and the tree under ``repo_root``."""

    def __init__(self, source: ProtocolSourceText | None = None) -> None:
        self._source = source or _DEFAULT_SOURCE

    def handle(self, request: ModelPathResolutionRequest) -> ModelPathResolutionResult:
        return ModelPathResolutionResult(
            selected_paths=tuple(
                _resolve(
                    list(request.changed_files),
                    request.adjacency,
                    Path(request.repo_root),
                    self._source,
                )
            )
        )
