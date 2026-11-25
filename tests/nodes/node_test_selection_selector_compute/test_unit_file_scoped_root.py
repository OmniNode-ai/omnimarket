# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A changed file under tests/unit/ selects that file and what imports or names it, not the directory.

One changed test file under tests/unit/ used to select the whole tests/unit/ directory, up to 5305
recorded seconds against the 1800 second narrowed bound, so the selection ran as the full suite. The
file-scoped root keeps every way a test could be affected: the importers of the changed file anywhere
in the tree (a test in another tests/unit folder is not lost), the tests whose text spells the changed
path, the surviving tests that name a deleted file, and the old directory when nothing can be attributed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
    clear_import_graph_cache,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayRequest,
    ModelReplayResult,
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyEntry,
    ModelAdjacencyMap,
    ModelPathResolutionRequest,
    ModelPathResolutionResult,
    ModelThresholds,
)

pytestmark = pytest.mark.unit

TREE = {
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    "tests/unit/a/__init__.py": "",
    "tests/unit/a/test_x.py": "def test_x() -> None: ...\n",
    "tests/unit/a/test_y.py": "def test_y() -> None: ...\n",
    "tests/unit/a/helper.py": "VALUE = 1\n",
    "tests/unit/a/test_uses_helper.py": "from tests.unit.a.helper import VALUE\n",
    "tests/unit/a/orphan_helper.py": "VALUE = 2\n",
    "tests/unit/a/data_helper.py": "VALUE = 3\n",
    "tests/unit/a/notes.txt": "data\n",
    "tests/unit/b/__init__.py": "",
    "tests/unit/b/test_other_folder.py": (
        "from tests.unit.a.helper import VALUE\n\ndef test_b() -> None: ...\n"
    ),
    "tests/unit/b/test_reads_path.py": (
        'SPELLED = "tests/unit/a/data_helper.py"\n\ndef test_r() -> None: ...\n'
    ),
    "tests/unit/b/test_unrelated.py": "def test_u() -> None: ...\n",
    "tests/unit/c/__init__.py": "",
    "tests/unit/c/test_names_gone.py": 'GONE = "tests/unit/a/test_gone.py"\n',
}
ALWAYS = "tests/gates/"


def _config(enabled: bool = True) -> ModelAdjacencyMap:
    return ModelAdjacencyMap(
        schema_version=1,
        shared_modules=[],
        thresholds=ModelThresholds(modules_changed_for_full_suite=6),
        test_infrastructure_paths=[".github/", "pyproject.toml", "uv.lock"],
        adjacency={"nodes": ModelAdjacencyEntry(), "unit": ModelAdjacencyEntry()},
        always_selected_paths=[ALWAYS],
        node_scoped_modules=["nodes"],
        unit_file_selection_enabled=enabled,
    )


def _resolve(
    root: Path, changed: list[str], enabled: bool = True
) -> ModelPathResolutionResult:
    clear_import_graph_cache()
    for rel, text in TREE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "tests/gates").mkdir(exist_ok=True)
    return HandlerTestPathResolution().handle(
        ModelPathResolutionRequest(
            changed_files=tuple(changed),
            adjacency=_config(enabled),
            repo_root=str(root),
        )
    )


def test_changed_unit_test_selects_the_file_not_the_directory(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/test_x.py"])
    assert "tests/unit/a/test_x.py" in result.selected_paths
    assert "tests/unit/" not in result.selected_paths
    assert "tests/unit/a/test_y.py" not in result.selected_paths
    assert result.full_suite_reason is None


def test_known_good_importer_in_another_unit_folder_is_not_lost(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/helper.py"])
    assert "tests/unit/b/test_other_folder.py" in result.selected_paths
    assert "tests/unit/a/test_uses_helper.py" in result.selected_paths
    assert "tests/unit/b/test_unrelated.py" not in result.selected_paths
    assert "tests/unit/" not in result.selected_paths


def test_known_bad_test_that_only_spells_the_path_is_caught_by_the_path_rule(
    tmp_path: Path,
) -> None:
    """A test that reads the changed file by path imports nothing; the old directory selection caught it."""
    result = _resolve(tmp_path, ["tests/unit/a/data_helper.py"])
    assert "tests/unit/b/test_reads_path.py" in result.selected_paths
    assert "tests/unit/" not in result.selected_paths


def test_known_bad_unattributable_helper_keeps_the_directory_and_names_it(
    tmp_path: Path,
) -> None:
    """A helper nothing imports and no test spells cannot be attributed: the old directory is selected."""
    result = _resolve(tmp_path, ["tests/unit/a/orphan_helper.py"])
    assert "tests/unit/" in result.selected_paths


def test_non_python_file_under_unit_keeps_the_directory(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/notes.txt"])
    assert "tests/unit/" in result.selected_paths


def test_package_marker_or_conftest_selects_its_own_directory(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/__init__.py"])
    assert "tests/unit/a/" in result.selected_paths
    assert "tests/unit/" not in result.selected_paths


def test_deleted_unit_test_selects_the_tests_that_name_it(tmp_path: Path) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/test_gone.py"])
    assert "tests/unit/c/test_names_gone.py" in result.selected_paths
    assert ALWAYS in result.selected_paths
    assert "tests/unit/" not in result.selected_paths


def test_switch_off_keeps_the_directory_for_any_changed_unit_file(
    tmp_path: Path,
) -> None:
    result = _resolve(tmp_path, ["tests/unit/a/test_x.py"], enabled=False)
    assert result.selected_paths == ("tests/unit/",)


def test_shipped_adjacency_turns_the_unit_file_rule_on() -> None:
    shipped = load_adjacency_map(
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "ci"
        / "test_selection_adjacency.yaml"
    )
    assert shipped.unit_file_selection_enabled is True


def _replay(enabled: bool) -> ModelReplayResult:
    """The recorded merges over the frozen tree, with the unit rule on or off in the frozen adjacency."""
    repo = Path(__file__).resolve().parents[3]
    document = json.loads(
        (repo / "config/test_selection_replay_input.json").read_text(encoding="utf-8")
    )
    document["repo_root"] = str(repo)

    class _Switched(HandlerSelectionReplay):
        @staticmethod
        def _snapshot(request: ModelReplayRequest) -> ModelTreeSnapshot:
            snapshot = HandlerSelectionReplay._snapshot(request)
            return snapshot.model_copy(
                update={
                    "adjacency": snapshot.adjacency.model_copy(
                        update={"unit_file_selection_enabled": enabled}
                    )
                }
            )

    result = _Switched().handle(ModelReplayRequest.model_validate(document))
    assert isinstance(result, ModelReplayResult), result
    return result


def test_replay_over_the_recorded_merges_narrows_two_more_with_the_rule_on() -> None:
    off = _replay(False)
    on = _replay(True)
    assert (off.totals.narrowed, off.totals.full_suite) == (10, 50)
    assert (on.totals.narrowed, on.totals.full_suite) == (12, 48)
    assert on.totals.selected_seconds < off.totals.selected_seconds
    changed = {
        d.sha
        for d in on.decisions
        if d.kind != next(o.kind for o in off.decisions if o.sha == d.sha)
    }
    assert len(changed) == 2
    assert all(d.kind == "narrowed" for d in on.decisions if d.sha in changed)


def test_a_second_deleted_file_does_not_reparse_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deleted-file lookup used to parse every test file again (about 22 s on the real tree).

    The shard-duration test makes dozens of such lookups in one process, so the repeated parse took
    the shard past its timeout. The parsed references are kept per file text: the first lookup
    parses, the next ones parse nothing.
    """
    from omnimarket.nodes.node_test_selection_selector_compute.handlers import (
        handler_test_path_resolution as handler,
    )

    _resolve(tmp_path, ["tests/unit/a/test_gone.py"])
    parses: list[int] = []
    real_parse = handler.ast.parse

    def counting_parse(*args: object, **kwargs: object) -> object:
        parses.append(1)
        return real_parse(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(handler.ast, "parse", counting_parse)
    result = HandlerTestPathResolution().handle(
        ModelPathResolutionRequest(
            changed_files=("tests/unit/a/test_other_gone.py",),
            adjacency=_config(),
            repo_root=str(tmp_path),
        )
    )
    assert ALWAYS in result.selected_paths
    assert parses == []
