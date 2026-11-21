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

A ``pyproject.toml`` change narrows by section when the adjacency map's ``pyproject_section_selection_enabled``
is on (OMN-26275): the changed keys come from a source-diff adapter (``data_file_changes``), and only a key
matching ``pyproject_narrowable_keys`` narrows, to the tests that read the file plus the closure of the entry
points that moved. Any other key, an unparseable side or a lock change beyond the package's own version line
is answered as ``full_suite_reason`` ``test_infrastructure``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_file_diff_git import (
    SourceFileDiffGit,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
    ModelDataFileChange,
    ModelKeyChange,
    ModelPathResolutionRequest,
    ModelPathResolutionResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_source_file_diff import (
    ProtocolSourceFileDiff,
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


ENTRY_POINT_KEY_PREFIX = "project.entry-points."
VERSION_KEY = "project.version"
# A read of installed entry-point metadata: importlib.metadata.entry_points(...), a distribution's
# .entry_points, pkg_resources iter_entry_points / load_entry_point. Reading other installed metadata
# (a distribution's direct_url.json) does not see a pyproject.toml entry point.
_ENTRY_POINT_READ = re.compile(r"\b(?:iter_|load_)?entry_points?\b")
_OPENS_FILE = re.compile(r"\btomllib\b|\btomli\b|\bread_text\(|\bread_bytes\(|\bopen\(")
_LOCK_OWN_VERSION_KEY = re.compile(r"^package\.(?P<name>.+)\.version$")


@dataclass(frozen=True)
class _PyprojectOutcome:
    """What a ``pyproject.toml`` change selects, or why it cannot narrow (OMN-26275)."""

    reason: EnumFullSuiteReason | None
    paths: frozenset[str] = frozenset()


_PYPROJECT_ESCALATES = _PyprojectOutcome(EnumFullSuiteReason.TEST_INFRASTRUCTURE)


def declared_data_file_changes(
    changed_files: list[str],
    config: ModelAdjacencyMap,
    repo_root: str,
    base_ref: str | None,
    head_ref: str = "HEAD",
    diff: ProtocolSourceFileDiff | None = None,
) -> tuple[ModelDataFileChange, ...]:
    """The changed keys of each declared project or lock file the diff touches (empty: not read).

    Empty when the section rule is off, no base ref is known or no declared file changed; the handler
    then escalates a ``pyproject.toml`` change rather than guess its keys.
    """
    declared = {*config.pyproject_paths, *config.pyproject_lock_paths}
    paths = [path for path in changed_files if path in declared]
    if not (config.pyproject_section_selection_enabled and base_ref and paths):
        return ()
    return (diff or SourceFileDiffGit()).changes(repo_root, base_ref, head_ref, paths)


def _key_narrows(key: str, config: ModelAdjacencyMap) -> bool:
    return any(
        fnmatchcase(key, pattern) for pattern in config.pyproject_narrowable_keys
    )


def _normalised_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_is_the_own_version_line(
    lock: ModelDataFileChange,
    keys: list[ModelKeyChange],
    project_text: str,
) -> bool:
    """True when the lock's only change is the package's own ``version`` line, matching the bump.

    The lock is compared structurally (``package`` tables keyed by name), so a reordered or reflowed
    lock is no change, and any other changed key, a package other than the project's own or a version
    that is not the pyproject bump keeps the full suite.
    """
    if lock.parse_failed or len(lock.changed_keys) != 1:
        return False
    bumps = [key for key in keys if key.key == VERSION_KEY]
    try:
        project_name = tomllib.loads(project_text)["project"]["name"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        return False
    change = lock.changed_keys[0]
    own = _LOCK_OWN_VERSION_KEY.match(change.key)
    return (
        len(bumps) == 1
        and own is not None
        and isinstance(project_name, str)
        and _normalised_name(own.group("name")) == _normalised_name(project_name)
        and change.base_value is not None
        and change.base_value == bumps[0].base_value
        and change.head_value == bumps[0].head_value
    )


def _target_module(value: str) -> str:
    """The module of an entry-point value (``pkg.mod:attr [extra]``)."""
    return re.split(r"[:\s\[;]", value.strip(), maxsplit=1)[0]


def _reverse_closure(graph: _ImportGraph, seeds: set[str]) -> set[str]:
    affected = set(seeds)
    frontier = list(seeds)
    while frontier:
        for dependent in graph.dependents.get(frontier.pop(), ()):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    return affected


def _tests_of(graph: _ImportGraph, affected: set[str]) -> set[str]:
    """Test files (and the conftest or package a test file loads through) among ``affected`` vertices."""
    selected: set[str] = set()
    for vertex in affected:
        test_path = graph.paths.get(vertex)
        if test_path is None:
            continue
        if _is_test_file(test_path):
            selected.add(test_path)
        elif test_path.rsplit("/", 1)[-1] in ("conftest.py", "__init__.py"):
            selected.add(test_path.rsplit("/", 1)[0] + "/")
    return selected


def _entry_point_closure(
    keys: list[ModelKeyChange],
    graph: _ImportGraph,
    node_scoped: frozenset[str],
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str]:
    """Tests reaching the modules an added, removed or repointed entry point names, on either side.

    The new target is read from the head tree. The old target (the base-side value) is seeded when it
    still exists; when the head tree no longer has it, the surviving tests that still name it are.
    """
    seeds: set[str] = set()
    for change in keys:
        if not change.key.startswith(ENTRY_POINT_KEY_PREFIX):
            continue
        for value in (change.base_value, change.head_value):
            if not value:
                continue
            target = _target_module(value)
            members = [
                name
                for name in graph.modules
                if name == target or name.startswith(f"{target}.")
            ]
            if members:
                seeds.update(_vertex(name, node_scoped) for name in members)
                continue
            for vertex, known_path in graph.paths.items():
                text = source.read_text(repo_root / known_path)
                if _names_old_file(
                    text, vertex, known_path.endswith("/__init__.py"), target
                ):
                    seeds.add(vertex)
    affected = _reverse_closure(graph, seeds)
    selected = _tests_of(graph, affected)
    for vertex in affected:
        module, _, name = vertex.partition("/")
        if not name or module not in node_scoped:
            continue
        if (repo_root / TEST_PREFIX / module / name).is_dir():
            selected.add(f"{TEST_PREFIX}{module}/{name}/")
        selected.update(p for p in graph.test_namers.get(name, ()) if _is_test_file(p))
    return selected


def _reads_project_file(text: str, project_paths: list[str]) -> bool:
    """A test file's text spells the project file, enumerates its directory or reads entry points."""
    return _reads_data_file(text, project_paths) or bool(_ENTRY_POINT_READ.search(text))


def _source_reads_project_file(text: str, project_paths: list[str]) -> bool:
    """A source file opens the project file or reads entry-point metadata.

    Spelling the file name is not a read (a docstring or a constant naming it): the file must also
    parse or open a file. A source reader pulls in everything that imports it, so a looser test would
    take in files that only mention the name.
    """
    spells = any(path.rsplit("/", 1)[-1] in text for path in project_paths)
    return bool(_ENTRY_POINT_READ.search(text)) or (
        spells and bool(_OPENS_FILE.search(text))
    )


@cache
def _file_dependents(
    repo_root: Path, source: ProtocolSourceText
) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """Dotted name -> repo path of every file, and dotted name -> the files that import it.

    File-level, unlike ``_import_graph`` whose vertex is a whole node package: a source file that reads
    entry-point metadata makes the files that import it readers, not every importer of its node.
    """
    files: dict[str, str] = {}
    packages: set[str] = set()
    for base in (repo_root / "src" / PACKAGE, repo_root / "tests"):
        for path in base.rglob("*.py"):
            rel = path.relative_to(repo_root).as_posix()
            files[_dotted(rel)] = rel
            if path.name == "__init__.py":
                packages.add(_dotted(rel))

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in files:
                return candidate
            parts.pop()
        return None

    dependents: defaultdict[str, set[str]] = defaultdict(set)
    for dotted, rel in files.items():
        for name in _references(
            source.read_text(repo_root / rel), dotted, dotted in packages
        ):
            target = resolve(name)
            if target is not None and target != dotted:
                dependents[target].add(dotted)
    return files, {k: frozenset(v) for k, v in dependents.items()}


def _importing_tests(
    seeds: set[str], repo_root: Path, source: ProtocolSourceText
) -> set[str]:
    """Test files that import any seed file, transitively (file-level)."""
    files, dependents = _file_dependents(repo_root, source)
    affected = set(seeds)
    frontier = list(seeds)
    while frontier:
        for dependent in dependents.get(frontier.pop(), ()):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    selected: set[str] = set()
    for dotted in affected:
        rel = files[dotted]
        if not rel.startswith(TEST_PREFIX):
            continue
        if _is_test_file(rel):
            selected.add(rel)
        elif rel.rsplit("/", 1)[-1] in ("conftest.py", "__init__.py"):
            selected.add(rel.rsplit("/", 1)[0] + "/")
    return selected


def _pyproject_readers(
    project_paths: list[str],
    config: ModelAdjacencyMap,
    graph: _ImportGraph,
    repo_root: Path,
    source: ProtocolSourceText,
) -> set[str] | None:
    """Tests that read the project file: by their own text, through a helper, or through a source file.

    A test whose text spells the file, enumerates its directory or reads entry-point metadata is a
    reader; so is every test that imports (transitively) such a helper under ``tests/`` or such a source
    file under ``pyproject_reader_globs``. ``data_reader_overrides`` adds the readers no text shows.
    None when a source file that reads the project file lies outside the import graph: its importers
    cannot be followed, so the caller escalates.
    """
    helpers: set[str] = set()
    for vertex, rel in graph.paths.items():
        if _reads_project_file(source.read_text(repo_root / rel), project_paths):
            helpers.add(vertex)
    selected = _tests_of(graph, _reverse_closure(graph, helpers))
    readers: set[str] = set()
    for pattern in config.pyproject_reader_globs:
        for path in sorted(repo_root.glob(pattern)):
            rel = path.relative_to(repo_root).as_posix()
            if not path.is_file() or rel.startswith(TEST_PREFIX):
                continue
            if not _source_reads_project_file(source.read_text(path), project_paths):
                continue
            files, _ = _file_dependents(repo_root, source)
            if not rel.endswith(".py") or _dotted(rel) not in files:
                return None
            readers.add(_dotted(rel))
    if readers:
        selected |= _importing_tests(readers, repo_root, source)
    for pattern, paths in config.data_reader_overrides.items():
        if any(fnmatchcase(path, pattern) for path in project_paths):
            selected.update(paths)
    return selected


def _pyproject_outcome(
    request: ModelPathResolutionRequest, source: ProtocolSourceText
) -> _PyprojectOutcome | None:
    """None when the rule does not apply (off, or no declared file changed); else what it decides."""
    config = request.adjacency
    if not config.pyproject_section_selection_enabled:
        return None
    changed_project = [f for f in request.changed_files if f in config.pyproject_paths]
    changed_lock = [
        f for f in request.changed_files if f in config.pyproject_lock_paths
    ]
    if not (changed_project or changed_lock):
        return None
    by_path = {change.path: change for change in request.data_file_changes}
    keys: list[ModelKeyChange] = []
    for path in changed_project:
        change = by_path.get(path)
        if change is None or change.parse_failed:
            return _PYPROJECT_ESCALATES
        keys.extend(change.changed_keys)
    if not changed_project or any(not _key_narrows(k.key, config) for k in keys):
        return _PYPROJECT_ESCALATES
    repo_root = Path(request.repo_root)
    for path in changed_lock:
        lock = by_path.get(path)
        project_text = source.read_text(repo_root / changed_project[0])
        if lock is None or not _lock_is_the_own_version_line(lock, keys, project_text):
            return _PYPROJECT_ESCALATES
    node_scoped = frozenset(config.node_scoped_modules)
    graph = _import_graph(repo_root, node_scoped, source)
    readers = _pyproject_readers(changed_project, config, graph, repo_root, source)
    if readers is None:
        return _PYPROJECT_ESCALATES
    selected = readers | _entry_point_closure(
        keys, graph, node_scoped, repo_root, source
    )
    return _PyprojectOutcome(
        None,
        frozenset(p for p in _admissible(selected) if (repo_root / p).exists()),
    )


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
        pyproject = _pyproject_outcome(request, self._source)
        if pyproject is not None:
            if pyproject.reason is not None:
                return ModelPathResolutionResult(
                    selected_paths=(), full_suite_reason=pyproject.reason
                )
            decided = {
                *request.adjacency.pyproject_paths,
                *request.adjacency.pyproject_lock_paths,
            }
            # Readers join a selection the change already has, or stand alone for a diff of only the
            # project and lock files; beside an unattributable file the full escalation stays.
            if selected or set(request.changed_files) <= decided:
                selected = sorted({*selected, *pyproject.paths})
        return ModelPathResolutionResult(
            selected_paths=tuple(selected), data_only=data_only
        )


# The graph's building blocks, public for the selection replay, which writes a frozen snapshot of
# a tree and reads it back as a stub tree this handler resolves over unchanged.
NODE_NAME = _NODE_NAME
ImportGraph = _ImportGraph
dotted_module = _dotted
module_references = _references
graph_vertex = _vertex


def clear_import_graph_cache() -> None:
    """Forget the import graphs built so far (one per checkout read)."""
    _import_graph.cache_clear()
