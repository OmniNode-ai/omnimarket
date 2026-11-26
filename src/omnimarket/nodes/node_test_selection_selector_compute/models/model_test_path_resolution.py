# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the test-path resolution handler and the module adjacency map (OMN-23719).

The adjacency map models live here; ``adapters/adjacency_file.py`` loads the YAML.
"""

from __future__ import annotations

from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
)


class EnumClosurePyprojectRule(StrEnum):
    """How a ``pyproject.toml`` or ``uv.lock`` change combines with the file-level closure."""

    #: Any change to a declared project or lock file takes the full suite (the closure reads neither).
    FULL_SUITE = "full_suite"
    #: The closure runs and the section rule adds its tests to the selection; an escalation of the
    #: section rule (a key off the allowlist, an unread diff, a lock change beyond the version line) wins.
    SEEDS = "seeds"


class ModelAdjacencyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reverse_deps: list[str] = Field(default_factory=list)


class ModelThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    modules_changed_for_full_suite: int = Field(..., ge=1)


class ModelPathTriggeredTests(BaseModel):
    """Tests a narrowed selection must also run when a changed file matches a glob."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_globs: list[str] = Field(..., min_length=1)
    """Repo-relative fnmatch patterns; one changed file matching any of them triggers."""
    selected_paths: list[str] = Field(..., min_length=1)
    """Test directories (trailing slash) or test files under tests/ to add."""

    @model_validator(mode="after")
    def validate_selected_paths(self) -> ModelPathTriggeredTests:
        for path in self.selected_paths:
            if not path.startswith("tests/") or not path.endswith(("/", ".py")):
                raise ValueError(
                    f"path_triggered_selected_paths entry '{path}' must be a "
                    "directory under tests/ with a trailing slash, or a test "
                    "file ending in .py"
                )
        return self


class ModelAdjacencyMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(..., ge=1)
    shared_modules: list[str]
    thresholds: ModelThresholds
    test_infrastructure_paths: list[str]
    full_suite_path_globs: list[Annotated[str, Field(strict=True)]] = Field(
        default_factory=list, strict=True
    )
    """Repo-relative fnmatch patterns whose changes require the full suite."""
    adjacency: dict[str, ModelAdjacencyEntry]
    always_selected_paths: list[str] = Field(default_factory=list)
    """Test directories and test files appended to EVERY narrowed selection.

    OMN-15639: a repo-wide gate (``tests/gates/``) asserts an invariant over
    files the adjacency map cannot attribute to one module -- e.g. the
    consumer-group declaration strip walks all 384 ``src/**/contract.yaml``.
    Under narrowing, changing one node's ``contract.yaml`` resolves to module
    ``nodes`` and selects only ``tests/nodes/``, so the repo-wide gate never
    runs and the invariant it protects is silently reintroducible on the
    everyday dev path. Entries here are unioned in after narrowing and are
    deliberately NOT filtered against the on-disk tree: a missing directory
    must surface as a loud pytest collection error, never as a silent drop
    that turns the gate vacuous.
    """

    path_triggered_selected_paths: list[ModelPathTriggeredTests] = Field(
        default_factory=list
    )
    """Tests appended to a narrowed selection when a changed file matches a glob.

    A repo-wide check that only a kind of change can break belongs here, not in
    ``always_selected_paths``: the node inventory test walks every node
    directory, contract and ``onex.nodes`` entry point, so a node contract
    change that narrowed to the node's own tests skipped it, and the guard that
    reads the same list as the drift gate went red only after the push. Like
    ``always_selected_paths`` the entries are NOT filtered against the on-disk
    tree: a missing test must surface as a loud pytest collection error.
    """

    node_scoped_modules: list[str] = Field(default_factory=list)
    """Modules made of node packages (``src/omnimarket/<module>/node_*/``).

    OMN-23719: selecting the whole ``tests/<module>/`` for one node's change
    summed past the selector's weight bound (1800.75 recorded seconds against
    1800 on dev), so every node change ran the full suite. For a module listed
    here the selector runs the changed nodes' tests, the tests of every node
    that imports them (transitively, through non-node modules too), the tests
    of nodes whose YAML names them, every test that imports or names them, and
    the module's flat ``tests/<module>/test_*.py`` files. When it cannot
    attribute a change (a deleted source, a non-Python file outside a node, a
    node with no tests of its own, a file that does not parse) it falls back to
    the whole ``tests/<module>/`` directory.
    """

    data_only_selection_enabled: bool = False
    """Whether a diff of only data files narrows instead of taking the full suite.

    A change whose files lie outside ``src/`` and ``tests/`` and all match ``data_only_globs`` used to
    reach no test directory and escalate as ``no_narrowable_selection``. With this on it selects the
    always set plus the tests that read the files. The rule changes what a pull request runs, so it is
    a switch the change that turns it on owns.
    """

    data_only_globs: list[str] = Field(default_factory=list)
    """Repo-relative fnmatch patterns of the data files the rule covers (ticket contracts, markdown)."""

    data_reader_overrides: dict[str, list[str]] = Field(default_factory=dict)
    """Data glob -> test files (or directories) that read a matching file in a way no test text shows."""

    data_file_readers_enabled: bool = False
    """Whether a changed fixture or data file selects the tests that read it instead of the full suite.

    For a changed non-Python file under one of ``data_reader_roots`` the readers are the tests whose text
    spells its path, its file name or a directory between a ``data_reader_fixture_roots`` entry and the
    file, the tests that enumerate its directory, the reverse import closure of any helper among those
    tests, the tests that import a ``src/`` file naming it, and ``data_reader_overrides``. A file with no
    reader selects the always set only. The rule changes what a pull request runs, so it is a switch the
    change that turns it on owns: off, a fixture under ``data_reader_gated_infrastructure_paths`` keeps the
    full-suite escalation it had.
    """

    data_reader_roots: list[str] = Field(default_factory=list)
    """Repo-relative prefixes under which a non-Python file is a data file the rule covers."""

    data_reader_fixture_roots: list[str] = Field(default_factory=list)
    """Prefixes below which the name of each directory between the prefix and the file also finds readers."""

    data_reader_gated_infrastructure_paths: list[str] = Field(default_factory=list)
    """Test infrastructure only while ``data_file_readers_enabled`` is off, or for a Python file.

    A non-Python data file under one of these paths is handled by the readers rule when it is on; with it
    off, and for any Python file beneath them, the path forces the full suite like ``test_infrastructure_paths``.
    """

    pyproject_section_selection_enabled: bool = False
    """Whether a ``pyproject.toml`` change narrows by section instead of taking the full suite.

    The stage 2 entry of the selection record. Off, a path in ``pyproject_paths`` or
    ``pyproject_lock_paths`` that ``test_infrastructure_paths`` names keeps the unconditional full-suite
    rule. On, the handler compares the changed keys of the file (``data_file_changes``) with
    ``pyproject_narrowable_keys`` and narrows only when every one matches.
    """

    unit_file_selection_enabled: bool = False
    """Whether a changed ``tests/unit/**/*.py`` file selects itself and what imports or names it.

    Off, any changed file under ``tests/unit/`` selects the whole ``tests/unit/`` directory (up to 5305
    recorded seconds, over the narrowed bound). On, the handler selects the file, its importers in any
    folder, the tests that spell a changed helper's path, and keeps the directory only for a change it
    cannot attribute. Off by default, so a frozen replay snapshot that predates the key answers as it did.
    """

    pyproject_paths: list[str] = Field(default_factory=list)
    """Repo-relative paths of the project files the section rule reads (``pyproject.toml``)."""

    pyproject_lock_paths: list[str] = Field(default_factory=list)
    """Repo-relative paths of their lock files (``uv.lock``): narrowable only beside a version change."""

    pyproject_narrowable_keys: list[str] = Field(default_factory=list)
    """Dotted key patterns (fnmatch) a ``pyproject.toml`` change may touch and still narrow.

    An allowlist: a changed key no pattern matches, including one the list has never seen, escalates.
    """

    pyproject_reader_globs: list[str] = Field(default_factory=list)
    """Repo-relative globs of the non-test files whose text is indexed for a read of the project file.

    A file that opens ``pyproject.toml`` or reads entry-point metadata (``importlib.metadata``,
    ``entry_points(``) makes every test that imports it, transitively, a reader.
    """

    closure_stage_pyproject_rule: EnumClosurePyprojectRule = (
        EnumClosurePyprojectRule.FULL_SUITE
    )
    """The rule for a project or lock file change at or above the policy's ``file_level_closure_stage``.

    The file-level closure reads no ``pyproject.toml``, so with ``pyproject_section_selection_enabled`` on
    the file would reach the closure as a file with no source. ``seeds`` (option b of the OMN-26487 choice) runs
    the closure and unions the section rule's tests, the allowlist ``pyproject_narrowable_keys`` and the
    lock's version-line rule included; ``full_suite``, the default for an absent entry, escalates the change.
    """

    def effective_test_infrastructure_paths(self) -> list[str]:
        """``test_infrastructure_paths`` less the files the section rule decides, when it is on."""
        if not self.pyproject_section_selection_enabled:
            return self.test_infrastructure_paths
        decided = {*self.pyproject_paths, *self.pyproject_lock_paths}
        return [p for p in self.test_infrastructure_paths if p not in decided]

    @model_validator(mode="after")
    def validate_data_reader_overrides(self) -> ModelAdjacencyMap:
        for glob, paths in self.data_reader_overrides.items():
            for path in paths:
                if not path.startswith("tests/") or not path.endswith(("/", ".py")):
                    raise ValueError(
                        f"data_reader_overrides['{glob}'] entry '{path}' must be a directory "
                        "under tests/ with a trailing slash, or a test file ending in .py"
                    )
        return self

    @model_validator(mode="after")
    def validate_node_scoped_modules(self) -> ModelAdjacencyMap:
        for module in self.node_scoped_modules:
            if module not in self.adjacency:
                raise ValueError(
                    f"node_scoped_modules entry '{module}' has no adjacency entry"
                )
        return self

    @model_validator(mode="after")
    def validate_always_selected_paths(self) -> ModelAdjacencyMap:
        for path in self.always_selected_paths:
            if not path.startswith("tests/") or not path.endswith(("/", ".py")):
                raise ValueError(
                    f"always_selected_paths entry '{path}' must be a directory "
                    "under tests/ with a trailing slash, or a test file ending in .py"
                )
        return self

    @model_validator(mode="after")
    def validate_shared_modules_in_adjacency(self) -> ModelAdjacencyMap:
        for shared in self.shared_modules:
            if shared not in self.adjacency:
                raise ValueError(f"shared_module '{shared}' has no adjacency entry")
        for module, entry in self.adjacency.items():
            for dep in entry.reverse_deps:
                if dep not in self.adjacency:
                    raise ValueError(
                        f"adjacency['{module}'].reverse_deps references unknown module '{dep}'"
                    )
        return self

    def is_reader_data_file(self, path: str) -> bool:
        """Whether the readers rule is on and ``path`` is a non-Python file under a data-reader root."""
        return (
            self.data_file_readers_enabled
            and not path.endswith(".py")
            and any(path.startswith(root) for root in self.data_reader_roots)
        )

    def is_test_infrastructure(self, path: str) -> bool:
        """Whether a changed ``path`` invalidates narrowing: the full suite runs."""
        paths = self.effective_test_infrastructure_paths()
        if not self.is_reader_data_file(path):
            paths = [*paths, *self.data_reader_gated_infrastructure_paths]
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)

    def narrowed_extra_paths(self, changed_files: list[str]) -> set[str]:
        """Always-selected tests plus those a matching changed file triggers."""
        triggered = {
            path
            for trigger in self.path_triggered_selected_paths
            if any(
                fnmatchcase(changed, pattern)
                for changed in changed_files
                for pattern in trigger.changed_globs
            )
            for path in trigger.selected_paths
        }
        return set(self.always_selected_paths) | triggered


class ModelKeyChange(BaseModel):
    """One changed key of a TOML file: its dotted path and its value on each side (None: absent)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    base_value: str | None = None
    head_value: str | None = None


class ModelDataFileChange(BaseModel):
    """The keys that differ between the base and head text of one declared data file.

    ``parse_failed`` is set when either side is absent or is not TOML: the keys are then unknown and the
    rule fails closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    parse_failed: bool = False
    changed_keys: tuple[ModelKeyChange, ...] = ()


class ModelPathResolutionRequest(BaseModel):
    """The changed files, the adjacency map and the repository root to read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changed_files: tuple[str, ...]
    adjacency: ModelAdjacencyMap
    repo_root: str = Field(min_length=1)
    data_file_changes: tuple[ModelDataFileChange, ...] = ()
    """The changed keys of each declared data file, from a source-diff adapter. Empty when not read."""


class ModelPathResolutionResult(BaseModel):
    """Repo-relative test paths that exist on disk, sorted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_paths: tuple[str, ...]
    data_only: bool = False
    """True when every changed file is a data file the adjacency map's data-only rule covers.

    Such a change may select no test of its own (the always set is added by the caller), so an empty
    ``selected_paths`` is a narrowed answer, not an unattributable one.
    """
    full_suite_reason: EnumFullSuiteReason | None = None
    """Set when the change cannot be narrowed (a ``pyproject.toml`` key outside the allowlist, an
    unparseable side, a lock change beyond the version line): the caller takes the full suite with it."""
