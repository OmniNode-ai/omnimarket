# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Maps a change's files to the test paths it can affect (OMN-23719).

scripts/ci/detect_test_paths.py used to hold this logic. The canonical-file-shape ratchet (OMN-20304)
lets a baselined script only shrink, so the mapping lives here, in the selector node, and the script
calls it. A module listed in the adjacency map's ``node_scoped_modules`` is narrowed by the import
graph: the changed nodes' tests, the tests of every node that imports them (transitively), the tests
of nodes whose YAML names them, and the tests that import or name them. A change the handler cannot
attribute falls back to the whole ``tests/<module>/`` directory.

A diff of only data files (the adjacency map's ``data_only_globs``: ticket contracts and markdown) selects
the tests that read them: a text index of the test files finds a test that spells the file or enumerates
its directory, directly or through an imported helper, and ``data_reader_overrides`` names the readers the
text cannot show. The caller adds the always set.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
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
_NOT_DATA_PREFIXES = ("src/", TEST_PREFIX)


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
    return _admissible(selected)


def _admissible(selected: set[str]) -> set[str]:
    """Selected paths in the shape TestPath admits, with files under a selected directory folded in.

    TestPath admits a file only as tests/[\\w/]+.py; any other file name is selected through its
    directory rather than dropped.
    """
    selected = {
        p if p.endswith("/") or _TEST_FILE_PATH.match(p) else p.rsplit("/", 1)[0] + "/"
        for p in selected
    }
    directories = tuple(p for p in selected if p.endswith("/"))
    return {p for p in selected if p.endswith("/") or not p.startswith(directories)}


def _is_data_file(path: str, config: ModelAdjacencyMap) -> bool:
    """A file outside src/ and tests/ that a ``data_only_globs`` pattern covers."""
    return not path.startswith(_NOT_DATA_PREFIXES) and any(
        fnmatchcase(path, pattern) for pattern in config.data_only_globs
    )


@cache
def _enumeration(directory: str) -> re.Pattern[str]:
    """Text that lists ``directory``: ``(root / "x").glob(``, ``x_dir.rglob(``, ``glob("x/..``."""
    name = re.escape(directory)
    return re.compile(
        rf"""{name}\w{{0,20}}["']?\s*\)?\s*(?:/\s*["'][^"']*["']\s*\)?)?\s*\.(?:r?glob|iterdir)\("""
        rf"""|(?:r?glob|listdir|scandir|walk)\(\s*[^)]{{0,80}}{name}"""
    )


def _reads_data_file(text: str, data_files: list[str]) -> bool:
    """Whether a test file's text spells a changed data file or enumerates the directory it is in."""
    for path in data_files:
        if path in text or path.rsplit("/", 1)[-1] in text:
            return True
        directories = path.split("/")[:-1]
        if any(_enumeration(directory).search(text) for directory in directories):
            return True
    return False


def _fixture_directory_names(files: list[str], config: ModelAdjacencyMap) -> set[str]:
    """Names of the directories between a ``data_reader_fixture_roots`` entry and each file."""
    return {
        directory
        for path in files
        for root in config.data_reader_fixture_roots
        if path.startswith(root)
        for directory in path[len(root) :].split("/")[:-1]
    }


def _source_seeds(
    files: list[str],
    graph: _ImportGraph,
    node_scoped: frozenset[str],
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str]:
    """Vertices of the ``src/`` files whose text spells a changed data file's path or name.

    The importers of these vertices read the file through them; the caller follows the reverse import
    closure. A path built at run time with no literal part is invisible here, by design: the nightly's.
    """
    # A file name inside a longer name (``setup_step_inventory.yaml`` for ``inventory.yaml``) is another file.
    names = re.compile(
        "|".join(
            rf"(?<![\w.\-]){re.escape(path.rsplit('/', 1)[-1])}(?!\w)" for path in files
        )
    )
    seeds: set[str] = set()
    for path in sorted((repo_root / "src" / PACKAGE).rglob("*.py")):
        text = source.read_text(path)
        if any(file in text for file in files) or names.search(text):
            rel = path.relative_to(repo_root).as_posix()
            seeds.add(_vertex(_dotted(rel), node_scoped))
    return {seed for seed in seeds if seed in graph.dependents or "/" in seed}


def _data_readers(
    data_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str]:
    """Test paths that read one of the changed data files.

    A test whose own text spells the file or enumerates its directory is a reader. So is every test
    that imports (transitively) a helper under ``tests/`` whose text does. The adjacency file's
    ``data_reader_overrides`` adds the readers no text shows. Where ``data_file_readers_enabled`` is
    true a data file under a ``data_reader_roots`` entry is also read by a test whose text names a
    directory between ``tests/fixtures/`` and the file, and through a ``src/`` file that names it, with
    the importers of that file.
    """
    node_scoped = frozenset(config.node_scoped_modules)
    graph = _import_graph(repo_root, node_scoped, source)
    reader_files = [path for path in data_files if config.is_reader_data_file(path)]
    directory_names = _fixture_directory_names(reader_files, config)
    selected: set[str] = set()
    helpers: set[str] = set()
    for vertex, rel in graph.paths.items():
        text = source.read_text(repo_root / rel)
        if not (
            _reads_data_file(text, data_files)
            or any(name in text for name in directory_names)
        ):
            continue
        if _is_test_file(rel):
            selected.add(rel)
        else:
            helpers.add(vertex)
    if reader_files:
        for seed in _source_seeds(reader_files, graph, node_scoped, repo_root, source):
            helpers.add(seed)
            module, _, node = seed.partition("/")
            if node and module in node_scoped:
                if (repo_root / TEST_PREFIX / module / node).is_dir():
                    selected.add(f"{TEST_PREFIX}{module}/{node}/")
                selected.update(
                    p for p in graph.test_namers.get(node, ()) if _is_test_file(p)
                )
    affected = set(helpers)
    frontier = list(helpers)
    while frontier:
        for dependent in graph.dependents.get(frontier.pop(), ()):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    for vertex in affected:
        test_path = graph.paths.get(vertex)
        if test_path is None:
            continue
        if _is_test_file(test_path):
            selected.add(test_path)
        elif test_path.rsplit("/", 1)[-1] in ("conftest.py", "__init__.py"):
            # pytest loads these for every test beneath them without an import.
            selected.add(test_path.rsplit("/", 1)[0] + "/")
    for pattern, paths in config.data_reader_overrides.items():
        if any(fnmatchcase(path, pattern) for path in data_files):
            selected.update(paths)
    return _admissible(selected)


def _names_old_file(text: str, module: str, is_package: bool, old: str) -> bool:
    """True when a file imports or spells a deleted file's dotted name or repo path."""
    old_path = f"{old.replace('.', '/')}.py"
    if old_path in text:
        return True
    return any(
        name == old or name.startswith(f"{old}.")
        for name in _references(text, module, is_package)
    )


def _root_level_selection(
    paths: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str]:
    """Test paths a change to root-level ``tests/*.py`` files can affect (OMN-26272).

    A changed root-level file selects itself (when it is a test file) and the tests that import it,
    transitively, from the import graph. A deleted or renamed one is no longer in the graph, so the
    surviving tests that still import its old dotted name (absolute or relative) or spell its old path
    are selected instead, with the always set. Reading the head tree finds them all: a test that
    named the old file and was not itself changed is unchanged at head, and one that was changed is
    selected as a changed file. ``tests/__init__.py`` and ``tests/conftest.py`` run for every test, so
    they keep the whole tree.
    """
    node_scoped = frozenset(config.node_scoped_modules)
    graph = _import_graph(repo_root, node_scoped, source)
    selected: set[str] = set()
    seeds: set[str] = set()
    removed: list[str] = []
    for path in paths:
        if path.rsplit("/", 1)[-1] in ("__init__.py", "conftest.py"):
            selected.add(TEST_PREFIX)
        elif (repo_root / path).is_file():
            vertex = graph.resolve(_dotted(path))
            if vertex is not None:
                seeds.add(vertex)
            if _is_test_file(path):
                selected.add(path)
        else:
            removed.append(path)
    if removed:
        selected.update(config.always_selected_paths)
        for test_vertex, known_path in graph.paths.items():
            text = source.read_text(repo_root / known_path)
            is_package = known_path.endswith("/__init__.py")
            if any(
                _names_old_file(text, test_vertex, is_package, _dotted(old))
                for old in removed
            ):
                seeds.add(test_vertex)

    affected = set(seeds)
    frontier = list(seeds)
    while frontier:
        for dependent in graph.dependents.get(frontier.pop(), ()):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    for affected_vertex in affected:
        test_path = graph.paths.get(affected_vertex)
        if test_path is None:
            continue
        if _is_test_file(test_path):
            selected.add(test_path)
        elif test_path.rsplit("/", 1)[-1] in ("conftest.py", "__init__.py"):
            selected.add(test_path.rsplit("/", 1)[0] + "/")
    return {
        p if p.endswith("/") or _TEST_FILE_PATH.match(p) else p.rsplit("/", 1)[0] + "/"
        for p in selected
    }


def _resolve(
    changed_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: Path,
    source: ProtocolSourceText,
) -> tuple[list[str], bool]:
    direct_modules: set[str] = set()
    selected: set[str] = set()
    node_scoped = frozenset(config.node_scoped_modules)
    scoped_test_modules: set[str] = set()
    root_level: list[str] = []

    reader_files = [p for p in changed_files if config.is_reader_data_file(p)]
    # a fixture under tests/fixtures/ has no tests beside it to select as a directory; its
    # readers are found by text below, and a file with none selects the always set only.
    fixture_files = {
        p
        for p in reader_files
        if p.startswith(tuple(config.data_reader_gated_infrastructure_paths))
    }

    for path in changed_files:
        if path in fixture_files:
            continue
        if path.startswith(SRC_PREFIX):
            module = path[len(SRC_PREFIX) :].split("/", 1)[0]
            if module in config.adjacency:
                direct_modules.add(module)
        elif path.startswith(TEST_PREFIX):
            parts = path.split("/")
            if len(parts) == 2 and path.endswith(".py"):
                # OMN-26272: a root-level file has no containing directory below
                # tests/, so it is selected as a file with its dependents below
                # rather than as a tests/<file>.py/ pseudo-directory (OMN-15277).
                root_level.append(path)
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

    if root_level:
        selected.update(_root_level_selection(root_level, config, repo_root, source))

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

    data_files = (
        [p for p in changed_files if _is_data_file(p, config)]
        if config.data_only_selection_enabled
        else []
    )
    # a data file under a reader root is looked up as well; one outside src/ and tests/
    # (a contract) or under tests/fixtures/ selects nothing of its own, so a diff of only these
    # stands alone like the T3 data files.
    standalone = {
        p
        for p in reader_files
        if p in fixture_files or not p.startswith(_NOT_DATA_PREFIXES)
    }
    lookup = list(dict.fromkeys([*data_files, *reader_files]))
    data_only = bool(changed_files) and {*data_files, *standalone} >= set(changed_files)
    result = sorted(p for p in selected if (repo_root / p).exists())
    # Readers join a selection the change already has, or stand alone for a diff of only data
    # files. A data file beside an unattributable one (a Dockerfile) keeps the full escalation.
    if lookup and (result or data_only):
        readers = _data_readers(lookup, config, repo_root, source)
        result = sorted(p for p in {*result, *readers} if (repo_root / p).exists())
    return result, data_only


class HandlerTestPathResolution:
    """Pure over its injected text source and the tree under ``repo_root``."""

    def __init__(self, source: ProtocolSourceText | None = None) -> None:
        self._source = source or _DEFAULT_SOURCE

    def handle(self, request: ModelPathResolutionRequest) -> ModelPathResolutionResult:
        selected, data_only = _resolve(
            list(request.changed_files),
            request.adjacency,
            Path(request.repo_root),
            self._source,
        )
        return ModelPathResolutionResult(
            selected_paths=tuple(selected), data_only=data_only
        )
