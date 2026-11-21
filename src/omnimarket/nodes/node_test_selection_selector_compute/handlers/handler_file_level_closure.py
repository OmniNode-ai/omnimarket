# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-level reverse import closure for every module, replacing the module-level escalations.

The closure ``HandlerTestPathResolution`` runs for node packages runs here for every changed file: a
vertex is a file, except that every file of one node package (``src/omnimarket/<module>/node_*/``) is
the one vertex ``<module>/<node>``. A changed file seeds the walk; the tests of every vertex the walk
reaches are selected. The closure covers:

- a deleted or renamed file: the surviving files that still name its old dotted name or path are
  seeds (a test that kept the old import fails loudly, which is the intent), as is its nearest
  surviving package;
- a ``conftest.py`` at any level: a dependency of every test below its directory, so a changed nested
  one, or a module one imports, selects that directory. The root ``tests/conftest.py`` is the one
  whose directory is the whole tree: ``root_conftest_edge`` keeps the edge (select the tree) or cuts it
  (select the closure's own tests and report ``collection_gate`` so the whole-tree collection job runs);
- a changed file the closure cannot place in the import graph: a typed ``unresolved_source``
  escalation, never a narrower guess.

The rule is active only at the policy's ``file_level_closure_stage``; below it
``legacy_module_escalation`` keeps the escalations the closure replaces.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    NODE_PREFIX,
    PACKAGE,
    SRC_PREFIX,
    TEST_PREFIX,
    _admissible,
    _data_readers,
    _dotted,
    _import_graph,
    _ImportGraph,
    _is_data_file,
    _is_test_file,
    _names_old_file,
    _package_seeds,
    _vertex,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_file_level_closure import (
    EnumClosureEscalation,
    EnumRootConftestEdge,
    ModelFileLevelClosureRequest,
    ModelFileLevelClosureResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
    ModelPathResolutionResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_source_text import (
    ProtocolSourceText,
)

_DEFAULT_SOURCE = SourceTextFile()
_LOADED_WITHOUT_AN_IMPORT = ("conftest.py", "__init__.py")


def legacy_module_escalation(
    changed_files: list[str], config: ModelAdjacencyMap
) -> EnumFullSuiteReason | None:
    """The module-level escalations the file-level closure replaces, kept below its stage.

    A source path with no mapped module, a change to a ``shared_modules`` module, and a count of
    changed modules at the adjacency file's threshold each take the full suite.
    """
    source_paths = [
        path[len(SRC_PREFIX) :] for path in changed_files if path.startswith(SRC_PREFIX)
    ]
    if any(
        "/" not in path or path.split("/", 1)[0] not in config.adjacency
        for path in source_paths
    ):
        return EnumFullSuiteReason.UNMAPPED_MODULE
    changed_modules = {path.split("/", 1)[0] for path in source_paths}
    if changed_modules & set(config.shared_modules):
        return EnumFullSuiteReason.SHARED_MODULE
    if len(changed_modules) >= config.thresholds.modules_changed_for_full_suite:
        return EnumFullSuiteReason.THRESHOLD_MODULES
    return None


def resolve_source_module(repo_root: Path, path: str) -> str | None:
    """Dotted module name of a ``src/omnimarket/`` Python file, or None when it cannot be placed.

    A file is placed when every directory from ``src/omnimarket`` down to its own carries an
    ``__init__.py``. A deleted file has no module of its own: it is placed at its nearest surviving
    package below ``omnimarket`` itself, and a deleted file directly under ``src/omnimarket`` is not
    placed (the package root would seed the whole tree).
    """
    if not path.startswith(SRC_PREFIX) or not path.endswith(".py"):
        return None
    directories = path[len(SRC_PREFIX) :].split("/")[:-1]
    package = repo_root / "src" / PACKAGE
    intact: list[Path] = []
    for name in (None, *directories):
        if name is not None:
            package = package / name
        if not (package / "__init__.py").is_file():
            break
        intact.append(package)
    if (repo_root / path).is_file():
        return _dotted(path) if len(intact) == 1 + len(directories) else None
    if len(intact) < 2:
        return None
    return _dotted(intact[-1].relative_to(repo_root).as_posix() + "/__init__.py")


def _node_scoped_modules(repo_root: Path) -> frozenset[str]:
    """Every module under ``src/omnimarket`` that holds a node package."""
    base = repo_root / "src" / PACKAGE
    if not base.is_dir():
        return frozenset()
    return frozenset(
        module.name
        for module in base.iterdir()
        if module.is_dir()
        and any(
            child.is_dir() and child.name.startswith(NODE_PREFIX)
            for child in module.iterdir()
        )
    )


def _referencers(
    removed: list[str],
    repo_root: Path,
    node_scoped: frozenset[str],
    source: ProtocolSourceText,
) -> set[str]:
    """Vertices of the files that still name a removed file's old dotted name or path."""
    found: set[str] = set()
    if not removed:
        return found
    olds = [_dotted(path) for path in removed]
    for base in (repo_root / "src" / PACKAGE, repo_root / "tests"):
        for file in base.rglob("*.py"):
            dotted = _dotted(file.relative_to(repo_root).as_posix())
            text = source.read_text(file)
            is_package = file.name == "__init__.py"
            if any(_names_old_file(text, dotted, is_package, old) for old in olds):
                found.add(_vertex(dotted, node_scoped))
    return found


class HandlerFileLevelClosure:
    """Pure over its injected text source and the tree under ``repo_root``."""

    def __init__(self, source: ProtocolSourceText | None = None) -> None:
        self._source = source or _DEFAULT_SOURCE

    def handle(
        self, request: ModelFileLevelClosureRequest
    ) -> ModelFileLevelClosureResult:
        repo_root = Path(request.repo_root)
        config = request.adjacency
        node_scoped = _node_scoped_modules(repo_root)
        graph = _import_graph(repo_root, node_scoped, self._source)
        selected: set[str] = set()
        seeds: set[str] = set()
        own_nodes: set[str] = set()
        removed: list[str] = []
        unresolved: list[str] = []

        for path in request.changed_files:
            if path.startswith(SRC_PREFIX):
                parts = path[len(SRC_PREFIX) :].split("/")
                if (
                    len(parts) >= 3
                    and parts[0] in node_scoped
                    and parts[1].startswith(NODE_PREFIX)
                ):
                    vertex = f"{parts[0]}/{parts[1]}"
                    if (repo_root / SRC_PREFIX / parts[0] / parts[1]).is_dir():
                        seeds.add(vertex)
                        own_nodes.add(vertex)
                    else:
                        removed.append(f"src/omnimarket/{parts[0]}/{parts[1]}.py")
                    continue
                module = resolve_source_module(repo_root, path)
                if module is None:
                    unresolved.append(path)
                    continue
                if not (repo_root / path).is_file():
                    removed.append(path)
                seeds.update(_package_seeds(graph, module, path, node_scoped))
            elif path.startswith(TEST_PREFIX):
                self._test_file(path, repo_root, graph, selected, seeds, removed)

        if unresolved:
            return ModelFileLevelClosureResult(
                escalation=EnumClosureEscalation.UNRESOLVED_SOURCE,
                unresolved_files=tuple(unresolved),
            )

        seeds |= _referencers(removed, repo_root, node_scoped, self._source)
        affected = set(seeds)
        frontier = list(seeds)
        while frontier:
            for dependent in graph.dependents.get(frontier.pop(), ()):
                if dependent not in affected:
                    affected.add(dependent)
                    frontier.append(dependent)

        collection_gate = self._select_closure(
            affected, seeds, own_nodes, repo_root, graph, selected, request
        )

        data_files = [p for p in request.changed_files if _is_data_file(p, config)]
        data_only = (
            config.data_only_selection_enabled
            and bool(request.changed_files)
            and len(data_files) == len(request.changed_files)
        )
        result = {p for p in _admissible(selected) if (repo_root / p).exists()}
        if config.data_only_selection_enabled and data_files and (result or data_only):
            result |= {
                p
                for p in _data_readers(data_files, config, repo_root, self._source)
                if (repo_root / p).exists()
            }
        return ModelFileLevelClosureResult(
            selected_paths=tuple(sorted(result)),
            data_only=data_only,
            collection_gate=collection_gate,
        )

    def _test_file(
        self,
        path: str,
        repo_root: Path,
        graph: _ImportGraph,
        selected: set[str],
        seeds: set[str],
        removed: list[str],
    ) -> None:
        name = path.rsplit("/", 1)[-1]
        depth = len(path.split("/"))
        if not path.endswith(".py"):
            selected.add(path.rsplit("/", 1)[0] + "/" if depth > 2 else TEST_PREFIX)
        elif depth == 2 and name in _LOADED_WITHOUT_AN_IMPORT:
            # tests/__init__.py and tests/conftest.py run for every test: the tree.
            selected.add(TEST_PREFIX)
        elif not (repo_root / path).is_file():
            removed.append(path)
        else:
            seed = graph.resolve(_dotted(path))
            if seed is not None:
                seeds.add(seed)
            if _is_test_file(path):
                selected.add(path)

    @staticmethod
    def _select_closure(
        affected: set[str],
        seeds: set[str],
        own_nodes: set[str],
        repo_root: Path,
        graph: _ImportGraph,
        selected: set[str],
        request: ModelFileLevelClosureRequest,
    ) -> bool:
        """Add the tests of every affected vertex to ``selected``; True when the collection gate runs."""
        paths = graph.paths
        dependents = graph.dependents
        yaml_namers = graph.yaml_namers
        test_namers = graph.test_namers
        collection_gate = False
        nodes = {v for v in affected if "/" in v}
        nodes |= {n for node in list(nodes) for n in yaml_namers.get(node, ())}

        for node in own_nodes:
            module, name = node.split("/", 1)
            has_tests = (repo_root / TEST_PREFIX / module / name).is_dir() or any(
                _is_test_file(paths[v]) for v in dependents.get(node, ()) if v in paths
            )
            if not has_tests and not any(
                _is_test_file(p) for p in test_namers.get(name, ())
            ):
                # a node with no test of its own cannot be narrowed: its module's tests run.
                selected.add(f"{TEST_PREFIX}{module}/")
        for module in {node.split("/", 1)[0] for node in own_nodes}:
            selected.update(
                p.relative_to(repo_root).as_posix()
                for p in (repo_root / TEST_PREFIX / module).glob("*.py")
                if _is_test_file(p.name)
            )
        for node in nodes:
            module, name = node.split("/", 1)
            if (repo_root / TEST_PREFIX / module / name).is_dir():
                selected.add(f"{TEST_PREFIX}{module}/{name}/")
            selected.update(p for p in test_namers.get(name, ()) if _is_test_file(p))
        for vertex in affected:
            test_path = paths.get(vertex)
            if test_path is None:
                continue
            if _is_test_file(test_path):
                selected.add(test_path)
            elif test_path.rsplit("/", 1)[-1] in _LOADED_WITHOUT_AN_IMPORT:
                directory = test_path.rsplit("/", 1)[0] + "/"
                if (
                    directory != TEST_PREFIX
                    or vertex in seeds
                    or request.root_conftest_edge is EnumRootConftestEdge.KEEP
                ):
                    selected.add(directory)
                else:
                    collection_gate = True
        # the whole-tree collection gate stands in for the tree only when the edge was cut
        return collection_gate and TEST_PREFIX not in selected


def closure_selection(
    changed_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
    root_conftest_edge: EnumRootConftestEdge,
) -> ModelPathResolutionResult | EnumFullSuiteReason:
    """The closure's test paths in the shape the older resolver answers, or the full-suite reason."""
    outcome = HandlerFileLevelClosure().handle(
        ModelFileLevelClosureRequest(
            changed_files=tuple(changed_files),
            adjacency=config,
            repo_root=str(repo_root),
            root_conftest_edge=root_conftest_edge,
        )
    )
    if outcome.escalation is not None:
        return EnumFullSuiteReason(outcome.escalation.value)
    return ModelPathResolutionResult(
        selected_paths=outcome.selected_paths, data_only=outcome.data_only
    )
