# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Change-aware test path resolution for omnimarket CI."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorRequest,
)
from scripts.ci.test_selection_loader import ModelAdjacencyMap, load_adjacency_map
from scripts.ci.test_selection_models import (
    EnumFullSuiteReason,
    ModelTestSelection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SRC_PREFIX = "src/omnimarket/"
TEST_PREFIX = "tests/"

FULL_SUITE_BRANCHES = {"main"}

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
def _import_graph(repo_root: Path, node_scoped: frozenset[str]) -> _ImportGraph:
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
        text = path.read_text(encoding="utf-8", errors="ignore")
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
                text = path.read_text(encoding="utf-8", errors="ignore")
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
) -> set[str] | None:
    """Test paths a change can affect inside node-scoped ``module`` (OMN-23719).

    None means the change cannot be attributed, and the caller selects the whole
    ``tests/<module>/`` directory instead.
    """
    graph = _import_graph(repo_root, node_scoped)
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


def resolve_test_paths(
    changed_files: list[str],
    adjacency_path: Path,
    repo_root: Path | None = None,
) -> list[str]:
    """Map changed file paths to deterministic test directories.

    Behavior:
      - Source changes under src/omnimarket/<module>: include tests/<module>/,
        except for a module in node_scoped_modules, where the tests of the
        nodes the change can reach by the import graph are selected instead
        (OMN-23719; see ModelAdjacencyMap.node_scoped_modules).
      - Test-only changes under tests/: include the changed test directory.
      - Files outside src/ and tests/: no contribution.
      - Mapped paths that do not exist on disk are dropped, so pytest never
        receives a non-existent test directory.
    """
    config = load_adjacency_map(adjacency_path)
    return _resolve(changed_files, config, repo_root=repo_root or REPO_ROOT)


def _requires_unnarrowable_full_suite(changed_files: list[str]) -> bool:
    """True when a changed path directly under tests/ cannot be narrowed.

    A path is "root-level" when it has exactly one path separator after the
    `tests/` prefix (i.e. `parts[1]` is the file itself, not a subdirectory
    name) -- `tests/test_foo.py`, not `tests/nodes/test_foo.py`. Such a path
    has no containing directory below `tests/` itself.

    Before this check, `_resolve()` built the string `tests/<parts[1]>/` for
    every tests/-prefixed change regardless of whether `parts[1]` names a
    directory or a file. For a root-level file that construction is wrong:
    it is either a non-existent pseudo-directory the on-disk filter silently
    drops (changed file absent at selection time -- e.g. deleted), or a
    `TestPath` value that is file-shaped rather than the directory-only shape
    the model documents (changed file present at selection time). Escalating
    to the real full suite, attributed by reason, replaces both outcomes with
    one deterministic and provable answer -- it does not depend on whether
    the file happens to exist on disk when the selector runs.
    """
    for path in changed_files:
        if not path.startswith(TEST_PREFIX) or not path.endswith(".py"):
            continue
        parts = path.split("/")
        if len(parts) == 2:
            return True
    return False


def _resolve(
    changed_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
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
                module, changed_files, node_scoped, repo_root
            )
            if narrowed is not None:
                selected.update(narrowed)
                continue
        selected.add(f"{TEST_PREFIX}{module}/")

    return sorted(p for p in selected if (repo_root / p).exists())


def compute_selection(
    changed_files: list[str],
    adjacency_path: Path,
    ref_name: str,
    event_name: str = "pull_request",
    feature_flag_enabled: bool = True,
    repo_root: Path | None = None,
) -> ModelTestSelection:
    config = load_adjacency_map(adjacency_path)
    root = repo_root or REPO_ROOT

    if not feature_flag_enabled:
        return _full_suite(EnumFullSuiteReason.FEATURE_FLAG_OFF)

    if ref_name in FULL_SUITE_BRANCHES:
        return _full_suite(EnumFullSuiteReason.MAIN_BRANCH)
    if event_name == "merge_group":
        return _full_suite(EnumFullSuiteReason.MERGE_GROUP)
    if event_name == "schedule":
        return _full_suite(EnumFullSuiteReason.SCHEDULED)

    for changed in changed_files:
        if any(
            changed == infra or changed.startswith(infra.rstrip("/") + "/")
            for infra in config.test_infrastructure_paths
        ):
            return _full_suite(EnumFullSuiteReason.TEST_INFRASTRUCTURE)

    # OMN-20180: delegation coverage spans unit, golden and flat tests, so
    # module-to-directory narrowing cannot safely represent these surfaces.
    if any(
        fnmatchcase(changed, pattern)
        for changed in changed_files
        for pattern in config.full_suite_path_globs
    ):
        return _full_suite(EnumFullSuiteReason.PROTECTED_SURFACE)

    # Fail closed even in mixed diffs: an unmapped source must not disappear
    # merely because another changed module contributed a test directory.
    # A file directly under src/omnimarket/ has no mapped module either.
    source_paths = [
        path[len(SRC_PREFIX) :] for path in changed_files if path.startswith(SRC_PREFIX)
    ]
    if any(
        "/" not in path or path.split("/", 1)[0] not in config.adjacency
        for path in source_paths
    ):
        return _full_suite(EnumFullSuiteReason.UNMAPPED_MODULE)

    # OMN-15277: a changed test path directly under tests/ root (no
    # subdirectory) cannot be narrowed below tests/ itself. Checked after the
    # test-infrastructure loop so paths already declared there (e.g.
    # tests/conftest.py) keep reporting TEST_INFRASTRUCTURE, not this reason.
    if _requires_unnarrowable_full_suite(changed_files):
        return _full_suite(EnumFullSuiteReason.CHANGED_TEST_UNNARROWABLE)

    changed_modules = {path.split("/", 1)[0] for path in source_paths}
    if changed_modules & set(config.shared_modules):
        return _full_suite(EnumFullSuiteReason.SHARED_MODULE)

    if len(changed_modules) >= config.thresholds.modules_changed_for_full_suite:
        return _full_suite(EnumFullSuiteReason.THRESHOLD_MODULES)

    selected = _resolve(changed_files, config, repo_root=root)
    if not selected:
        # OMN-20305: nothing narrowed, so the whole tests/ tree runs. Take the
        # full-suite shard matrix; an is_full_suite=False selection of tests/
        # became one serial Split 1/1 job (63 minutes on omnimarket#3175).
        return _full_suite(EnumFullSuiteReason.NO_NARROWABLE_SELECTION)
    # OMN-15639: repo-wide gates are unioned in after narrowing. They assert
    # invariants over files the adjacency map cannot attribute to a single
    # module (all of src/**/contract.yaml, for instance), so a narrowed run
    # that omits them would let the invariant be reintroduced on the everyday
    # dev path. Not filtered against the on-disk tree -- see the field
    # docstring on ModelAdjacencyMap.always_selected_paths.
    if not any(path == TEST_PREFIX for path in selected):
        selected = sorted(set(selected) | set(config.always_selected_paths))
    # OMN-23296: shard count and weight bound come from estimated test time.
    decision = HandlerTestSelectionSelector(None, load_policy()).handle(
        ModelSelectorRequest(selected_paths=tuple(selected), repo_root=str(root))
    )
    if decision.split_count is None:
        return _full_suite(EnumFullSuiteReason.NARROWED_SELECTION_TOO_HEAVY)

    return ModelTestSelection(
        selected_paths=selected,
        split_count=decision.split_count,
        is_full_suite=False,
        full_suite_reason=None,
        matrix=list(range(1, decision.split_count + 1)),
    )


def _full_suite(reason: EnumFullSuiteReason) -> ModelTestSelection:
    return ModelTestSelection(
        selected_paths=["tests/"],
        split_count=20,
        is_full_suite=True,
        full_suite_reason=reason,
        matrix=list(range(1, 21)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve change-aware test paths")
    parser.add_argument(
        "--changed-files-from",
        type=Path,
        required=True,
        help="Path to a file with one changed-file path per line.",
    )
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--event-name", default="pull_request")
    parser.add_argument(
        "--adjacency",
        type=Path,
        default=Path(__file__).parent / "test_selection_adjacency.yaml",
    )
    parser.add_argument(
        "--feature-flag",
        choices=("on", "off"),
        default="on",
    )
    args = parser.parse_args(argv)

    changed = [
        line.strip()
        for line in args.changed_files_from.read_text().splitlines()
        if line.strip()
    ]
    selection = compute_selection(
        changed_files=changed,
        adjacency_path=args.adjacency,
        ref_name=args.ref_name,
        event_name=args.event_name,
        feature_flag_enabled=(args.feature_flag == "on"),
    )
    sys.stdout.write(selection.model_dump_json())
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
