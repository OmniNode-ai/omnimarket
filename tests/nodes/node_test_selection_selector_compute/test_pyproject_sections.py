# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pyproject.toml change narrows by section: only an allowlisted changed key narrows.

The selector compares the base and head text of pyproject.toml key by key (the git source-diff adapter,
with an in-memory twin here). A change whose every key matches ``pyproject_narrowable_keys`` selects the
tests that read the file (found in test texts and in the source files that open it or read entry-point
metadata, followed through their importers) plus the closure of the entry points that moved; any other
key, an unknown one, an unparseable side or a lock change beyond the package's own version line takes the
full suite with reason ``test_infrastructure``. The rule is off until the adjacency map's switch is on.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_file_diff_git import (
    SourceFileDiffGit,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_file_diff_in_memory import (
    SourceFileDiffInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_file_level_closure import (
    HandlerFileLevelClosure,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_file_level_closure import (
    EnumRootConftestEdge,
    ModelFileLevelClosureRequest,
    ModelFileLevelClosureResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyEntry,
    ModelAdjacencyMap,
    ModelDataFileChange,
    ModelKeyChange,
    ModelPathResolutionRequest,
    ModelPathResolutionResult,
    ModelThresholds,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
)
from scripts.ci.detect_test_paths import compute_selection

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
ALWAYS = "tests/gates/"
NARROWABLE = [
    "project.entry-points.onex.nodes.*",
    "project.version",
    "tool.onex.dashboard_bundle.*",
]
READER_GLOBS = ["src/omnimarket/**/*.py"]

BASE_PYPROJECT = textwrap.dedent(
    """\
    [build-system]
    requires = ["hatchling"]

    [project]
    name = "omnimarket"
    version = "0.4.1"
    dependencies = ["pydantic>=2.11.0"]

    [project.entry-points."onex.nodes"]
    node_a = "omnimarket.nodes.node_a"
    node_m1 = "omnimarket.nodes.node_m1"

    [tool.pytest.ini_options]
    markers = ["unit"]

    [tool.onex.dashboard_bundle]
    title = "market"
    """
)

TREE = {
    "pyproject.toml": BASE_PYPROJECT,
    "src/omnimarket/__init__.py": "",
    "src/omnimarket/nodes/__init__.py": "",
    "src/omnimarket/nodes/node_a/__init__.py": "",
    "src/omnimarket/nodes/node_a/handler.py": "X = 1\n",
    "src/omnimarket/nodes/node_b/__init__.py": "",
    "src/omnimarket/nodes/node_b/handler.py": "Y = 1\n",
    "src/omnimarket/nodes/node_m1/__init__.py": "",
    "src/omnimarket/nodes/node_m1/handler.py": "M = 1\n",
    "src/omnimarket/nodes/node_m2/__init__.py": "",
    "src/omnimarket/nodes/node_m2/handler.py": "M = 2\n",
    "src/omnimarket/config/__init__.py": "",
    "src/omnimarket/config/loader.py": (
        "from importlib.metadata import entry_points\n\n\n"
        "def nodes() -> list[object]:\n"
        '    return list(entry_points(group="onex.nodes"))\n'
    ),
    "tests/__init__.py": "",
    "tests/gates/__init__.py": "",
    "tests/gates/test_gate.py": "def test_gate() -> None: ...\n",
    "tests/helper_reads.py": 'TEXT = open("pyproject.toml").read()\n',
    "tests/test_spells_pyproject.py": 'PATH = "pyproject.toml"\n',
    "tests/test_reads_metadata.py": (
        "import importlib.metadata\n\n\n"
        "def test_nodes() -> None:\n"
        '    assert importlib.metadata.entry_points(group="onex.nodes")\n'
    ),
    "tests/test_via_helper.py": "from tests.helper_reads import TEXT\n",
    "tests/test_via_source_reader.py": "from omnimarket.config.loader import nodes\n",
    "tests/test_override_only.py": "def test_hidden() -> None: ...\n",
    "tests/test_unrelated.py": "def test_unrelated() -> None: ...\n",
    "tests/nodes/__init__.py": "",
    "tests/nodes/node_a/__init__.py": "",
    "tests/nodes/node_a/test_a.py": ("from omnimarket.nodes.node_a.handler import X\n"),
    "tests/nodes/node_b/__init__.py": "",
    "tests/nodes/node_b/test_b.py": ("from omnimarket.nodes.node_b.handler import Y\n"),
    "tests/nodes/node_m1/__init__.py": "",
    "tests/nodes/node_m1/test_m1.py": (
        "from omnimarket.nodes.node_m1.handler import M\n"
    ),
    "tests/nodes/node_m2/__init__.py": "",
    "tests/nodes/node_m2/test_m2.py": (
        "from omnimarket.nodes.node_m2.handler import M\n"
    ),
}

READERS = {
    "tests/test_spells_pyproject.py",
    "tests/test_reads_metadata.py",
    "tests/test_via_helper.py",
    "tests/test_via_source_reader.py",
    "tests/test_override_only.py",
}


def _config(
    *,
    narrowable: list[str] | None = None,
    reader_globs: list[str] | None = None,
    enabled: bool = True,
) -> ModelAdjacencyMap:
    return ModelAdjacencyMap(
        schema_version=1,
        shared_modules=[],
        thresholds=ModelThresholds(modules_changed_for_full_suite=6),
        test_infrastructure_paths=[".github/", "pyproject.toml", "uv.lock"],
        adjacency={"nodes": ModelAdjacencyEntry(), "config": ModelAdjacencyEntry()},
        always_selected_paths=[ALWAYS],
        node_scoped_modules=["nodes"],
        data_reader_overrides={"pyproject.toml": ["tests/test_override_only.py"]},
        pyproject_section_selection_enabled=enabled,
        pyproject_paths=["pyproject.toml"],
        pyproject_lock_paths=["uv.lock"],
        pyproject_narrowable_keys=NARROWABLE if narrowable is None else narrowable,
        pyproject_reader_globs=READER_GLOBS if reader_globs is None else reader_globs,
    )


def _write_tree(root: Path, head_pyproject: str, lock_head: str | None = None) -> None:
    for rel, text in TREE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "pyproject.toml").write_text(head_pyproject)
    if lock_head is not None:
        (root / "uv.lock").write_text(lock_head)


def _lock(own: str = "0.4.1", other: str = "2.11.0", python: str = ">=3.12") -> str:
    return textwrap.dedent(
        f"""\
        version = 1
        requires-python = "{python}"

        [[package]]
        name = "omnimarket"
        version = "{own}"
        source = {{ editable = "." }}

        [[package]]
        name = "pydantic"
        version = "{other}"
        """
    )


def _resolve(
    root: Path,
    changed: list[str],
    *,
    base: str = BASE_PYPROJECT,
    head: str = BASE_PYPROJECT,
    lock: tuple[str, str] | None = None,
    config: ModelAdjacencyMap | None = None,
    changes: tuple[ModelDataFileChange, ...] | None = None,
) -> ModelPathResolutionResult:
    """The handler over a fixture tree whose head pyproject is ``head`` and whose diff is in memory."""
    _write_tree(root, head, lock[1] if lock else None)
    texts: dict[str, tuple[str | None, str | None]] = {"pyproject.toml": (base, head)}
    if lock:
        texts["uv.lock"] = lock
    config = config or _config()
    if changes is None:
        changes = SourceFileDiffInMemory(texts).changes(
            "", "base", "head", [*config.pyproject_paths, *config.pyproject_lock_paths]
        )
    return HandlerTestPathResolution().handle(
        ModelPathResolutionRequest(
            changed_files=tuple(changed),
            adjacency=config,
            repo_root=str(root),
            data_file_changes=changes,
        )
    )


ADD_NODE_B = BASE_PYPROJECT.replace(
    'node_m1 = "omnimarket.nodes.node_m1"\n',
    'node_m1 = "omnimarket.nodes.node_m1"\nnode_b = "omnimarket.nodes.node_b"\n',
)
BUMPED = BASE_PYPROJECT.replace('version = "0.4.1"', 'version = "0.4.2"')
REPOINTED = BASE_PYPROJECT.replace(
    'node_m1 = "omnimarket.nodes.node_m1"', 'node_m1 = "omnimarket.nodes.node_m2"'
)
ADDOPTS = BASE_PYPROJECT.replace(
    'markers = ["unit"]', 'markers = ["unit"]\naddopts = "-p no:randomly"'
)


# --- the allowlisted sections narrow --------------------------------------------------------------


def test_pyproject_sections_added_entry_point_selects_readers_and_the_new_node(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=ADD_NODE_B)
    assert result.full_suite_reason is None
    selected = set(result.selected_paths)
    assert selected >= READERS
    assert "tests/nodes/node_b/" in selected
    assert "tests/test_unrelated.py" not in selected
    assert "tests/" not in selected


def test_pyproject_sections_version_bump_selects_the_readers_only(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=BUMPED)
    assert result.full_suite_reason is None
    assert set(result.selected_paths) == READERS


def test_pyproject_sections_dashboard_bundle_key_narrows(tmp_path: Path) -> None:
    head = BASE_PYPROJECT.replace('title = "market"', 'title = "market"\nicon = "x"')
    result = _resolve(tmp_path, ["pyproject.toml"], head=head)
    assert result.full_suite_reason is None
    assert set(result.selected_paths) >= READERS


def test_pyproject_sections_comment_only_change_narrows_to_the_readers(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head="# note\n" + BASE_PYPROJECT)
    assert result.full_suite_reason is None
    assert set(result.selected_paths) >= READERS


def test_pyproject_sections_a_test_reading_entry_points_through_importlib_metadata_is_selected(
    tmp_path: Path,
) -> None:
    # The test never spells pyproject.toml: only its entry-point metadata read finds it.
    assert "pyproject" not in TREE["tests/test_reads_metadata.py"]
    result = _resolve(tmp_path, ["pyproject.toml"], head=ADD_NODE_B)
    assert "tests/test_reads_metadata.py" in result.selected_paths


def test_pyproject_sections_a_test_reaching_pyproject_through_a_helper_is_selected(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=ADD_NODE_B)
    assert "tests/test_via_helper.py" in result.selected_paths


def test_pyproject_sections_a_test_reaching_a_source_reader_is_selected_and_the_index_is_the_cause(
    tmp_path: Path,
) -> None:
    selected = _resolve(tmp_path, ["pyproject.toml"], head=ADD_NODE_B).selected_paths
    assert "tests/test_via_source_reader.py" in selected
    # Control: with no source-side reader index the same test is left out.
    without = _resolve(
        tmp_path / "control",
        ["pyproject.toml"],
        head=ADD_NODE_B,
        config=_config(reader_globs=[]),
    ).selected_paths
    assert "tests/test_via_source_reader.py" not in without
    assert "tests/test_reads_metadata.py" in without


def test_pyproject_sections_a_reader_named_only_in_the_overrides_is_selected(
    tmp_path: Path,
) -> None:
    selected = _resolve(tmp_path, ["pyproject.toml"], head=BUMPED).selected_paths
    assert "tests/test_override_only.py" in selected


def test_pyproject_sections_repointed_entry_point_selects_both_targets(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=REPOINTED)
    assert result.full_suite_reason is None
    selected = set(result.selected_paths)
    assert "tests/nodes/node_m1/" in selected
    assert "tests/nodes/node_m2/" in selected
    assert "tests/nodes/node_a/" not in selected


def test_pyproject_sections_dropping_the_base_side_read_loses_the_old_target(
    tmp_path: Path,
) -> None:
    changes = SourceFileDiffInMemory({"pyproject.toml": (BASE_PYPROJECT, REPOINTED)})
    full = changes.changes("", "base", "head", ["pyproject.toml"])
    key = "project.entry-points.onex.nodes.node_m1"
    assert [(c.key, c.base_value) for c in full[0].changed_keys] == [
        (key, "omnimarket.nodes.node_m1")
    ]
    # Control: the same change with the base-side value dropped selects the new target only.
    head_only = (
        ModelDataFileChange(
            path="pyproject.toml",
            changed_keys=(
                ModelKeyChange(
                    key=key, base_value=None, head_value="omnimarket.nodes.node_m2"
                ),
            ),
        ),
    )
    selected = set(
        _resolve(
            tmp_path, ["pyproject.toml"], head=REPOINTED, changes=head_only
        ).selected_paths
    )
    assert "tests/nodes/node_m2/" in selected
    assert "tests/nodes/node_m1/" not in selected


def test_pyproject_sections_removed_entry_point_selects_survivors_naming_the_old_module(
    tmp_path: Path,
) -> None:
    head = BASE_PYPROJECT.replace('node_m1 = "omnimarket.nodes.node_m1"\n', "")
    texts = {"pyproject.toml": (BASE_PYPROJECT, head)}
    changes = SourceFileDiffInMemory(texts).changes(
        "", "base", "head", ["pyproject.toml"]
    )
    _write_tree(tmp_path, head)
    # node_m1 is gone from the head tree; a surviving test still names its old dotted module.
    for gone in ("__init__.py", "handler.py"):
        (tmp_path / "src/omnimarket/nodes/node_m1" / gone).unlink()
    (tmp_path / "tests/nodes/node_m1/test_m1.py").unlink()
    (tmp_path / "tests/test_names_old.py").write_text(
        'TARGET = "omnimarket.nodes.node_m1"\n'
    )
    result = HandlerTestPathResolution().handle(
        ModelPathResolutionRequest(
            changed_files=("pyproject.toml",),
            adjacency=_config(),
            repo_root=str(tmp_path),
            data_file_changes=changes,
        )
    )
    assert result.full_suite_reason is None
    assert "tests/test_names_old.py" in result.selected_paths


# --- every other key escalates --------------------------------------------------------------------


def test_pyproject_unknown_key_escalates(tmp_path: Path) -> None:
    head = BASE_PYPROJECT + '\n[tool.example]\nsetting = "on"\n'
    result = _resolve(tmp_path, ["pyproject.toml"], head=head)
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE
    assert result.selected_paths == ()
    # Falsifier: with `tool.example.setting` on the allowlist the same change narrows.
    allowed = _resolve(
        tmp_path / "control",
        ["pyproject.toml"],
        head=head,
        config=_config(narrowable=[*NARROWABLE, "tool.example.setting"]),
    )
    assert allowed.full_suite_reason is None


@pytest.mark.parametrize(
    ("label", "head"),
    [
        (
            "dependencies",
            BASE_PYPROJECT.replace(
                'dependencies = ["pydantic>=2.11.0"]',
                'dependencies = ["pydantic>=2.11.0", "temporalio>=1.35.0"]',
            ),
        ),
        ("build_system", BASE_PYPROJECT.replace("hatchling", "setuptools")),
        (
            "tool_uv_sources",
            BASE_PYPROJECT
            + '\n[tool.uv.sources]\nomnibase-infra = { git = "x", tag = "t" }\n',
        ),
        (
            "tool_uv_override",
            BASE_PYPROJECT + '\n[tool.uv]\noverride-dependencies = ["a>=1"]\n',
        ),
        ("tool_setuptools", BASE_PYPROJECT + '\n[tool.setuptools]\npackages = ["x"]\n'),
        (
            "version_beside_dependency",
            BUMPED.replace("pydantic>=2.11.0", "pydantic>=2.12.0"),
        ),
    ],
)
def test_pyproject_sections_keys_outside_the_allowlist_escalate(
    label: str, head: str, tmp_path: Path
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=head)
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE, label


def test_pyproject_sections_injected_pytest_addopts_escalates(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=ADDOPTS)
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE
    # Falsifier: the same key moved into an allowlisted section narrows, so the test above is the
    # allowlist's doing and not a blanket escalation.
    moved = BASE_PYPROJECT.replace(
        'title = "market"', 'title = "market"\naddopts = "-p no:randomly"'
    )
    narrowed = _resolve(tmp_path / "control", ["pyproject.toml"], head=moved)
    assert narrowed.full_suite_reason is None


@pytest.mark.parametrize("side", ["base", "head"])
def test_pyproject_unparseable_escalates(side: str, tmp_path: Path) -> None:
    broken = "[project\nname = "
    base, head = (broken, BUMPED) if side == "base" else (BASE_PYPROJECT, broken)
    result = _resolve(tmp_path, ["pyproject.toml"], base=base, head=head)
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE
    assert result.selected_paths == ()


@pytest.mark.parametrize("missing", ["base", "head"])
def test_pyproject_unparseable_escalates_when_a_side_is_missing(
    missing: str, tmp_path: Path
) -> None:
    texts: dict[str, tuple[str | None, str | None]] = {
        "pyproject.toml": (None, BUMPED)
        if missing == "base"
        else (BASE_PYPROJECT, None)
    }
    changes = SourceFileDiffInMemory(texts).changes("", "b", "h", ["pyproject.toml"])
    result = _resolve(tmp_path, ["pyproject.toml"], head=BUMPED, changes=changes)
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_pyproject_unparseable_escalates_when_the_diff_was_not_read(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["pyproject.toml"], head=BUMPED, changes=())
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_pyproject_sections_switch_off_leaves_the_handler_unchanged(
    tmp_path: Path,
) -> None:
    result = _resolve(
        tmp_path, ["pyproject.toml"], head=ADD_NODE_B, config=_config(enabled=False)
    )
    assert result.full_suite_reason is None
    assert result.selected_paths == ()


# --- uv.lock ----------------------------------------------------------------------------------------


def test_pyproject_sections_version_with_the_packages_own_lock_line_narrows(
    tmp_path: Path,
) -> None:
    result = _resolve(
        tmp_path,
        ["pyproject.toml", "uv.lock"],
        head=BUMPED,
        lock=(_lock("0.4.1"), _lock("0.4.2")),
    )
    assert result.full_suite_reason is None
    assert set(result.selected_paths) >= READERS


@pytest.mark.parametrize(
    ("label", "lock_head"),
    [
        ("dependency_version", _lock("0.4.2", other="2.12.0")),
        ("requires_python", _lock("0.4.2", python=">=3.13")),
        ("own_version_not_the_bump", _lock("0.4.9")),
        ("unparseable", "[[package\nname ="),
    ],
)
def test_pyproject_sections_a_lock_change_beyond_the_own_version_line_stays_full(
    label: str, lock_head: str, tmp_path: Path
) -> None:
    result = _resolve(
        tmp_path,
        ["pyproject.toml", "uv.lock"],
        head=BUMPED,
        lock=(_lock("0.4.1"), lock_head),
    )
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE, label


def test_pyproject_sections_a_lock_change_without_a_version_change_stays_full(
    tmp_path: Path,
) -> None:
    result = _resolve(
        tmp_path,
        ["pyproject.toml", "uv.lock"],
        head=ADD_NODE_B,
        lock=(_lock("0.4.1"), _lock("0.4.1", other="2.12.0")),
    )
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_pyproject_sections_a_lock_change_alone_stays_full(tmp_path: Path) -> None:
    result = _resolve(
        tmp_path,
        ["uv.lock"],
        lock=(_lock("0.4.1"), _lock("0.4.2")),
        head=BASE_PYPROJECT,
    )
    assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


# --- the diff adapters ------------------------------------------------------------------------------


def test_pyproject_sections_adapter_reports_added_removed_and_changed_keys() -> None:
    head = BASE_PYPROJECT.replace('version = "0.4.1"', 'version = "0.4.2"').replace(
        'node_m1 = "omnimarket.nodes.node_m1"\n',
        'node_c = "omnimarket.nodes.node_c"\n',
    )
    changes = SourceFileDiffInMemory(
        {"pyproject.toml": (BASE_PYPROJECT, head)}
    ).changes("", "b", "h", ["pyproject.toml"])
    assert len(changes) == 1
    assert changes[0].parse_failed is False
    assert {(c.key, c.base_value, c.head_value) for c in changes[0].changed_keys} == {
        ("project.version", "0.4.1", "0.4.2"),
        ("project.entry-points.onex.nodes.node_m1", "omnimarket.nodes.node_m1", None),
        ("project.entry-points.onex.nodes.node_c", None, "omnimarket.nodes.node_c"),
    }


def test_pyproject_sections_adapter_sees_a_type_change_as_a_change() -> None:
    base = "[tool.x]\nlimit = 1\n"
    head = '[tool.x]\nlimit = "1"\n'
    changes = SourceFileDiffInMemory({"pyproject.toml": (base, head)}).changes(
        "", "b", "h", ["pyproject.toml"]
    )
    assert [c.key for c in changes[0].changed_keys] == ["tool.x.limit"]


_GIT_LOCATION_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
)


def scrub_git_location_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the git location variables a hook exports before shelling out to git (OMN-14891)."""
    scrubbed = {k: v for k, v in env.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def _git_repo(
    root: Path, base: str, head: str, lock: tuple[str, str] | None = None
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "pyproject.toml").write_text(base)
    tracked = ["pyproject.toml"]
    if lock:
        (root / "uv.lock").write_text(lock[0])
        tracked.append("uv.lock")
    _git(root, "add", *tracked)
    _git(root, "commit", "-q", "-m", "base")
    (root / "pyproject.toml").write_text(head)
    if lock:
        (root / "uv.lock").write_text(lock[1])
    _git(root, "commit", "-q", "--allow-empty", "-am", "head")


def test_pyproject_sections_git_adapter_matches_the_in_memory_twin(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path, BASE_PYPROJECT, REPOINTED)
    from_git = SourceFileDiffGit().changes(
        str(tmp_path), "HEAD~1", "HEAD", ["pyproject.toml"]
    )
    from_memory = SourceFileDiffInMemory(
        {"pyproject.toml": (BASE_PYPROJECT, REPOINTED)}
    ).changes("", "b", "h", ["pyproject.toml"])
    assert from_git == from_memory


def test_pyproject_sections_git_adapter_reports_an_unknown_ref_as_a_parse_failure(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path, BASE_PYPROJECT, BUMPED)
    changes = SourceFileDiffGit().changes(
        str(tmp_path), "no-such-ref", "HEAD", ["pyproject.toml"]
    )
    assert changes[0].parse_failed is True


# --- the shipped settings and the selector around the handler -------------------------------------


def test_pyproject_sections_shipped_adjacency_declares_the_rule_and_keeps_it_off() -> (
    None
):
    config = load_adjacency_map(SHIPPED_ADJACENCY)
    assert config.pyproject_narrowable_keys == NARROWABLE
    assert config.pyproject_reader_globs
    assert config.pyproject_paths == ["pyproject.toml"]
    assert config.pyproject_lock_paths == ["uv.lock"]
    # The rule is the stage 2 entry: nothing a pull request runs at stage 0 or 1 changes with it.
    assert config.pyproject_section_selection_enabled is False
    assert "pyproject.toml" in config.test_infrastructure_paths
    assert (
        config.effective_test_infrastructure_paths() == config.test_infrastructure_paths
    )


def test_pyproject_sections_a_pyproject_change_stays_full_while_the_rule_is_off() -> (
    None
):
    selection = compute_selection(["pyproject.toml"], SHIPPED_ADJACENCY, "feature")
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def _selector_fixture(
    root: Path,
    head: str,
    *,
    closure_rule: str | None = None,
    narrowable: list[str] | None = None,
    lock: tuple[str, str] | None = None,
) -> Path:
    _write_tree(root, BASE_PYPROJECT)
    _git_repo(root, BASE_PYPROJECT, head, lock)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "test_file_durations.json").write_text(
        json.dumps({rel: 1.0 for rel in TREE if rel.startswith("tests/")})
    )
    adjacency = root / "adjacency.yaml"
    data = json.loads(_config(narrowable=narrowable).model_dump_json())
    if closure_rule is not None:
        data["closure_stage_pyproject_rule"] = closure_rule
    adjacency.write_text(yaml.safe_dump(data))
    return adjacency


def test_pyproject_sections_selector_narrows_through_the_git_diff(
    tmp_path: Path,
) -> None:
    adjacency = _selector_fixture(tmp_path, ADD_NODE_B)
    selection = compute_selection(
        ["pyproject.toml"], adjacency, "feature", repo_root=tmp_path, base_ref="HEAD~1"
    )
    assert not selection.is_full_suite
    assert set(selection.selected_paths) >= READERS
    assert ALWAYS in selection.selected_paths


def test_pyproject_sections_selector_escalates_an_unknown_key_with_test_infrastructure(
    tmp_path: Path,
) -> None:
    head = BASE_PYPROJECT + '\n[tool.example]\nsetting = "on"\n'
    adjacency = _selector_fixture(tmp_path, head)
    selection = compute_selection(
        ["pyproject.toml"], adjacency, "feature", repo_root=tmp_path, base_ref="HEAD~1"
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_pyproject_sections_selector_without_a_base_ref_escalates(
    tmp_path: Path,
) -> None:
    adjacency = _selector_fixture(tmp_path, ADD_NODE_B)
    selection = compute_selection(
        ["pyproject.toml"], adjacency, "feature", repo_root=tmp_path
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_pyproject_sections_a_ci_change_beside_pyproject_still_takes_the_full_suite(
    tmp_path: Path,
) -> None:
    adjacency = _selector_fixture(tmp_path, ADD_NODE_B)
    selection = compute_selection(
        ["pyproject.toml", ".github/workflows/ci.yml"],
        adjacency,
        "feature",
        repo_root=tmp_path,
        base_ref="HEAD~1",
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


# --- the replay of the eighteen pyproject merges ----------------------------------------------------

#: The 60 dev merges of the baseline window that changed pyproject.toml, 18 of them. Each row holds the
#: verbatim statements of the changed keys from the real base and head text (and, for the merges that
#: changed uv.lock, the lock's differing tables), the key list of the whole file and the other
#: infrastructure files the merge changed.
MERGES = json.loads(
    (Path(__file__).parent / "pyproject_merges.json").read_text(encoding="utf-8")
)
DEPENDENCY_MERGES = {"cdbc99eb4a", "d970dcb251", "c62d5b24d5"}
#: The merges whose pyproject keys all narrow but whose lock changes more than the own version line.
LOCK_BEYOND_VERSION_MERGES = {"79f31e93f5"}
#: Narrow here; another rule (.github/ or scripts/ is test infrastructure) takes the full suite.
OTHER_RULE_MERGES = {"84bd917586", "b28b1c210e"}


def _merge_expectation(sha: str) -> str:
    if sha in DEPENDENCY_MERGES | LOCK_BEYOND_VERSION_MERGES:
        return "full"
    return "narrow"


def test_pyproject_sections_replay_covers_the_eighteen_merges() -> None:
    assert len(MERGES) == 18
    assert {row["sha"][:10] for row in MERGES} >= DEPENDENCY_MERGES


@pytest.mark.parametrize("row", MERGES, ids=[row["sha"][:10] for row in MERGES])
def test_pyproject_sections_replay_of_each_pyproject_merge(
    row: dict[str, object], tmp_path: Path
) -> None:
    sha = str(row["sha"])[:10]
    lock = row["lock"]
    assert lock is None or isinstance(lock, dict)
    result = _resolve(
        tmp_path,
        ["pyproject.toml", *(["uv.lock"] if lock else [])],
        base=str(row["base"]),
        head=str(row["head"]),
        lock=(str(lock["base"]), str(lock["head"])) if lock else None,
    )
    if _merge_expectation(sha) == "full":
        assert result.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE, sha
        assert result.selected_paths == ()
    else:
        assert result.full_suite_reason is None, sha
        assert set(result.selected_paths) >= READERS, sha
        assert "tests/" not in result.selected_paths


def test_pyproject_sections_replay_splits_three_dependency_one_lock_and_fourteen_narrow(
    tmp_path: Path,
) -> None:
    outcomes = {}
    for index, row in enumerate(MERGES):
        lock = row["lock"]
        result = _resolve(
            tmp_path / str(index),
            ["pyproject.toml", *(["uv.lock"] if lock else [])],
            base=str(row["base"]),
            head=str(row["head"]),
            lock=(str(lock["base"]), str(lock["head"])) if lock else None,
        )
        outcomes[str(row["sha"])[:10]] = result.full_suite_reason is None
    narrowed = {sha for sha, narrow in outcomes.items() if narrow}
    assert {sha for sha, narrow in outcomes.items() if not narrow} == (
        DEPENDENCY_MERGES | LOCK_BEYOND_VERSION_MERGES
    )
    assert len(narrowed) == 14


def test_pyproject_sections_replay_merges_with_a_ci_or_script_change_stay_full_by_the_other_rule() -> (
    None
):
    config = load_adjacency_map(SHIPPED_ADJACENCY)
    other = {
        str(row["sha"])[:10]: row["other_infra"] for row in MERGES if row["other_infra"]
    }
    assert set(other) == OTHER_RULE_MERGES
    for sha, files in other.items():
        assert isinstance(files, list)
        assert any(
            str(f).startswith(infra)
            for f in files
            for infra in config.effective_test_infrastructure_paths()
        ), sha


# --- the closure stage: the same section rule, read from the adjacency file's closure entry ----------
#
# At or above the policy's file_level_closure_stage the file-level closure replaces the module-level
# path, and it reads no pyproject.toml. The adjacency map's ``closure_stage_pyproject_rule`` rules how a
# declared project file's change combines with it: ``seeds`` runs the closure and adds the section
# rule's tests to its selection (an escalation of the section rule still wins), ``full_suite`` (the
# default, an absent entry) takes the full suite for any pyproject.toml or uv.lock change.

CLOSURE_STAGE = 3
SRC_CHANGE = "src/omnimarket/nodes/node_a/handler.py"
UNKNOWN_KEY_HEAD = BASE_PYPROJECT + '\n[tool.example]\nsetting = "on"\n'


def _shipped_closure_rule() -> str:
    """The rule the shipped adjacency file declares: a KeyError when the entry is removed."""
    shipped = yaml.safe_load(SHIPPED_ADJACENCY.read_text(encoding="utf-8"))
    return str(shipped["closure_stage_pyproject_rule"])


def _closure_stage_selection(
    root: Path,
    changed: list[str],
    head: str,
    *,
    rule: str | None,
    narrowable: list[str] | None = None,
    base_ref: str | None = "HEAD~1",
) -> tuple[bool, str | None, tuple[str, ...]]:
    """compute_selection at the closure stage, twice: the two answers must be the same."""
    adjacency = _selector_fixture(root, head, closure_rule=rule, narrowable=narrowable)
    answers = [
        compute_selection(
            changed,
            adjacency,
            "feature",
            repo_root=root,
            base_ref=base_ref,
            stage=CLOSURE_STAGE,
        )
        for _ in range(2)
    ]
    assert answers[0] == answers[1]
    selection = answers[0]
    reason = selection.full_suite_reason
    return (
        selection.is_full_suite,
        reason.value if reason is not None else None,
        tuple(selection.selected_paths),
    )


def test_closure_stage_pyproject_rule_shipped_adjacency_declares_seeds() -> None:
    config = load_adjacency_map(SHIPPED_ADJACENCY)
    assert _shipped_closure_rule() == "seeds"
    assert config.closure_stage_pyproject_rule.value == "seeds"
    # the rule reuses the OMN-26275 allowlist and lock rule, it declares no key list of its own
    assert config.pyproject_narrowable_keys == NARROWABLE
    assert config.pyproject_lock_paths == ["uv.lock"]


def test_closure_stage_pyproject_rule_seeds_select_the_readers_at_the_closure_stage(
    tmp_path: Path,
) -> None:
    full, reason, selected = _closure_stage_selection(
        tmp_path, ["pyproject.toml"], ADD_NODE_B, rule=_shipped_closure_rule()
    )
    assert not full, reason
    assert set(selected) >= READERS
    assert "tests/nodes/node_b/" in selected
    assert ALWAYS in selected
    assert "tests/test_unrelated.py" not in selected
    assert "tests/" not in selected


def test_closure_stage_pyproject_rule_removed_entry_takes_the_full_suite(
    tmp_path: Path,
) -> None:
    # the control of the test above: the same change with the entry removed is not narrowed
    full, reason, _ = _closure_stage_selection(
        tmp_path, ["pyproject.toml"], ADD_NODE_B, rule=None
    )
    assert full
    assert reason == "test_infrastructure"


def test_closure_stage_pyproject_rule_full_suite_entry_escalates_beside_a_source_change(
    tmp_path: Path,
) -> None:
    # with the switch on and no seeds entry, the closure of the source file must not narrow the diff
    full, reason, _ = _closure_stage_selection(
        tmp_path, ["pyproject.toml", SRC_CHANGE], ADD_NODE_B, rule=None
    )
    assert full
    assert reason == "test_infrastructure"


def test_closure_stage_pyproject_rule_seeds_join_the_closure_of_a_source_change(
    tmp_path: Path,
) -> None:
    full, reason, selected = _closure_stage_selection(
        tmp_path,
        ["pyproject.toml", SRC_CHANGE],
        ADD_NODE_B,
        rule=_shipped_closure_rule(),
    )
    assert not full, reason
    assert set(selected) >= READERS
    assert "tests/nodes/node_a/" in selected
    assert "tests/nodes/node_b/" in selected


def _locked_selector_fixture(root: Path, head: str, lock: tuple[str, str]) -> Path:
    """A git fixture whose base commit holds ``lock[0]`` and whose head commit holds ``lock[1]``."""
    return _selector_fixture(
        root, head, closure_rule=_shipped_closure_rule(), lock=lock
    )


def test_closure_stage_pyproject_rule_version_bump_with_the_own_lock_line_narrows(
    tmp_path: Path,
) -> None:
    adjacency = _locked_selector_fixture(
        tmp_path, BUMPED, (_lock("0.4.1"), _lock("0.4.2"))
    )
    selection = compute_selection(
        ["pyproject.toml", "uv.lock"],
        adjacency,
        "feature",
        repo_root=tmp_path,
        base_ref="HEAD~1",
        stage=CLOSURE_STAGE,
    )
    assert not selection.is_full_suite
    assert set(selection.selected_paths) >= READERS


def test_closure_stage_pyproject_rule_a_lock_change_beyond_the_version_line_escalates(
    tmp_path: Path,
) -> None:
    adjacency = _locked_selector_fixture(
        tmp_path, BUMPED, (_lock("0.4.1"), _lock("0.4.2", other="2.12.0"))
    )
    selection = compute_selection(
        ["pyproject.toml", "uv.lock"],
        adjacency,
        "feature",
        repo_root=tmp_path,
        base_ref="HEAD~1",
        stage=CLOSURE_STAGE,
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_closure_stage_pyproject_rule_a_lock_change_alone_escalates(
    tmp_path: Path,
) -> None:
    adjacency = _locked_selector_fixture(
        tmp_path, BASE_PYPROJECT, (_lock("0.4.1"), _lock("0.4.2"))
    )
    selection = compute_selection(
        ["uv.lock"],
        adjacency,
        "feature",
        repo_root=tmp_path,
        base_ref="HEAD~1",
        stage=CLOSURE_STAGE,
    )
    assert selection.is_full_suite
    assert selection.full_suite_reason is EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_closure_stage_pyproject_unknown_key_escalates(tmp_path: Path) -> None:
    # the fixture key is tool.example.setting: it is on no allowlist, so it takes the full suite
    shipped = load_adjacency_map(SHIPPED_ADJACENCY)
    assert not any(
        fnmatchcase("tool.example.setting", pattern)
        for pattern in shipped.pyproject_narrowable_keys
    )
    full, reason, _ = _closure_stage_selection(
        tmp_path / "alone",
        ["pyproject.toml"],
        UNKNOWN_KEY_HEAD,
        rule=_shipped_closure_rule(),
    )
    assert full
    assert reason == "test_infrastructure"
    # an unknown key beside a source file is not narrowed away by the source file's closure
    full, reason, _ = _closure_stage_selection(
        tmp_path / "beside_source",
        ["pyproject.toml", SRC_CHANGE],
        UNKNOWN_KEY_HEAD,
        rule=_shipped_closure_rule(),
    )
    assert full
    assert reason == "test_infrastructure"
    # Falsifier: with tool.example.setting on the allowlist the same change narrows
    full, reason, selected = _closure_stage_selection(
        tmp_path / "control",
        ["pyproject.toml"],
        UNKNOWN_KEY_HEAD,
        rule=_shipped_closure_rule(),
        narrowable=[*NARROWABLE, "tool.example.setting"],
    )
    assert not full, reason
    assert set(selected) >= READERS


def test_closure_stage_pyproject_unknown_key_escalates_without_a_base_ref(
    tmp_path: Path,
) -> None:
    full, reason, _ = _closure_stage_selection(
        tmp_path,
        ["pyproject.toml"],
        ADD_NODE_B,
        rule=_shipped_closure_rule(),
        base_ref=None,
    )
    assert full
    assert reason == "test_infrastructure"


def _closure_resolve(
    root: Path,
    changed: list[str],
    *,
    base: str,
    head: str,
    lock: tuple[str, str] | None,
) -> ModelFileLevelClosureResult:
    """The closure handler over a fixture tree whose diff is in memory, with the shipped rule."""
    _write_tree(root, head, lock[1] if lock else None)
    texts: dict[str, tuple[str | None, str | None]] = {"pyproject.toml": (base, head)}
    if lock:
        texts["uv.lock"] = lock
    config = _config().model_copy(
        update={"closure_stage_pyproject_rule": _shipped_closure_rule()}
    )
    changes = SourceFileDiffInMemory(texts).changes(
        "", "base", "head", [*config.pyproject_paths, *config.pyproject_lock_paths]
    )
    return HandlerFileLevelClosure().handle(
        ModelFileLevelClosureRequest(
            changed_files=tuple(changed),
            adjacency=config,
            repo_root=str(root),
            root_conftest_edge=EnumRootConftestEdge.CUT,
            data_file_changes=changes,
        )
    )


def test_closure_stage_pyproject_sections_replay_splits_three_dependency_one_lock_fourteen_narrow(
    tmp_path: Path,
) -> None:
    narrowed: set[str] = set()
    full: set[str] = set()
    for index, row in enumerate(MERGES):
        lock = row["lock"]
        changed = ["pyproject.toml", *(["uv.lock"] if lock else [])]
        kwargs = {
            "base": str(row["base"]),
            "head": str(row["head"]),
            "lock": (str(lock["base"]), str(lock["head"])) if lock else None,
        }
        result = _closure_resolve(tmp_path / str(index), changed, **kwargs)
        again = _closure_resolve(tmp_path / f"{index}_again", changed, **kwargs)
        assert result == again
        sha = str(row["sha"])[:10]
        if result.escalation is None:
            narrowed.add(sha)
            assert set(result.selected_paths) >= READERS, sha
            assert "tests/" not in result.selected_paths, sha
        else:
            full.add(sha)
            assert result.selected_paths == (), sha
            assert result.escalation.value == "test_infrastructure", sha
    assert full == DEPENDENCY_MERGES | LOCK_BEYOND_VERSION_MERGES
    assert full >= DEPENDENCY_MERGES
    assert len(narrowed) == 14
