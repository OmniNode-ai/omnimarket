# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A changed root-level test file selects itself and its dependents (OMN-26272).

``tests/`` holds flat root-level modules. A change to one used to take the whole suite (reason
``changed_test_unnarrowable``). The resolution handler now selects the changed file and the tests that
import it, and for a deleted or renamed root-level file the always set plus every surviving test that
still names its old path or old dotted name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyEntry,
    ModelAdjacencyMap,
    ModelPathResolutionRequest,
    ModelThresholds,
)

pytestmark = pytest.mark.unit

ALWAYS = "tests/gates/"

ADJACENCY = ModelAdjacencyMap(
    schema_version=1,
    shared_modules=[],
    thresholds=ModelThresholds(modules_changed_for_full_suite=6),
    test_infrastructure_paths=[],
    adjacency={"nodes": ModelAdjacencyEntry()},
    always_selected_paths=[ALWAYS],
    node_scoped_modules=["nodes"],
)

# The four merges the baseline replay of the last 60 dev merges attributed to
# changed_test_unnarrowable: each one's root-level changed files.
ROOT_TEST_FILES_OF_THE_FOUR_UNNARROWABLE_MERGES = {
    "4430263d9b3524f35e285d327a7a0b07667b4523": (
        "tests/test_golden_chain_private_hosted_minutes_watch_effect.py",
    ),
    "5addb723ab70802a5393a3e97c5312767bb24a2e": (
        "tests/test_host_reconcile_failure_alarm.py",
    ),
    "56fd1a76431ed7fea5a31cb8673fa1b7c712d152": (
        "tests/test_worktree_reconcile_core_worktree.py",
        "tests/test_worktree_reconcile_schedule.py",
    ),
    "2b0f4e88f30fb61c4ae77a026f958e72252c7e44": (
        "tests/test_golden_chain_github_repo_write_effect.py",
    ),
}


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> None:
    """A flat tests/ with a helper, two importers (absolute and relative) and a bystander."""
    _write(root, "tests/__init__.py")
    _write(root, "tests/gates/test_gate.py", "def test_g() -> None: ...\n")
    _write(root, "tests/helper.py", "def f() -> int:\n    return 1\n")
    _write(
        root,
        "tests/test_x.py",
        "from .helper import f\n\n\ndef test_x() -> None:\n    assert f()\n",
    )
    _write(
        root,
        "tests/test_abs.py",
        "from tests.helper import f\n\n\ndef test_abs() -> None:\n    assert f()\n",
    )
    _write(root, "tests/test_bystander.py", "def test_b() -> None: ...\n")


def _select(root: Path, *changed: str) -> tuple[str, ...]:
    return (
        HandlerTestPathResolution()
        .handle(
            ModelPathResolutionRequest(
                changed_files=changed, adjacency=ADJACENCY, repo_root=str(root)
            )
        )
        .selected_paths
    )


def test_root_test_file_change_selects_the_changed_file_and_its_importers(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    selected = _select(tmp_path, "tests/helper.py")
    assert "tests/test_x.py" in selected
    assert "tests/test_abs.py" in selected
    assert "tests/test_bystander.py" not in selected
    assert "tests/" not in selected


def test_root_test_file_change_selects_itself(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert _select(tmp_path, "tests/test_bystander.py") == ("tests/test_bystander.py",)


def test_root_test_file_selection_covers_the_four_unnarrowable_merges(
    tmp_path: Path,
) -> None:
    files = {
        f
        for group in ROOT_TEST_FILES_OF_THE_FOUR_UNNARROWABLE_MERGES.values()
        for f in group
    }
    assert len(ROOT_TEST_FILES_OF_THE_FOUR_UNNARROWABLE_MERGES) == 4
    _write(tmp_path, "tests/__init__.py")
    for f in files:
        _write(tmp_path, f, "def test_it() -> None: ...\n")
    for merge, changed in ROOT_TEST_FILES_OF_THE_FOUR_UNNARROWABLE_MERGES.items():
        selected = _select(tmp_path, *changed)
        for path in changed:
            assert path in selected, f"{merge}: {path} missing from {selected!r}"


def test_root_test_file_selection_follows_importers_transitively(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(
        tmp_path,
        "tests/test_chain.py",
        "from tests.test_x import test_x\n\n\ndef test_c() -> None:\n    test_x()\n",
    )
    assert "tests/test_chain.py" in _select(tmp_path, "tests/helper.py")


def test_root_test_file_deleted_never_yields_a_pseudo_directory(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    selected = _select(tmp_path, "tests/test_gone.py")
    assert not any(p.endswith(".py/") for p in selected)
    assert "tests/test_gone.py" not in selected
    assert ALWAYS in selected


def test_root_test_file_deleted_helper_selects_the_importing_tests(
    tmp_path: Path,
) -> None:
    """AC3: tests/helper.py is deleted; tests/test_x.py (relative import) and tests/test_abs.py
    still import it, so both are selected with the always set."""
    _tree(tmp_path)
    (tmp_path / "tests/helper.py").unlink()
    selected = _select(tmp_path, "tests/helper.py")
    assert "tests/test_x.py" in selected
    assert "tests/test_abs.py" in selected
    assert "tests/test_bystander.py" not in selected
    assert ALWAYS in selected


def test_root_test_file_renamed_helper_selects_tests_naming_the_old_name(
    tmp_path: Path,
) -> None:
    """A rename is the old path deleted and the new path added in one change."""
    _tree(tmp_path)
    (tmp_path / "tests/helper.py").rename(tmp_path / "tests/helper_renamed.py")
    selected = _select(tmp_path, "tests/helper.py", "tests/helper_renamed.py")
    assert "tests/test_x.py" in selected
    assert "tests/test_abs.py" in selected
    assert ALWAYS in selected


def test_root_test_file_deleted_helper_selects_a_test_spelling_its_old_path(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(
        tmp_path,
        "tests/test_reader.py",
        'from pathlib import Path\n\nTEXT = Path("tests/helper.py").read_text()\n',
    )
    (tmp_path / "tests/helper.py").unlink()
    assert "tests/test_reader.py" in _select(tmp_path, "tests/helper.py")


def test_root_test_file_package_init_and_conftest_keep_the_whole_tree(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    _write(tmp_path, "tests/conftest.py")
    assert _select(tmp_path, "tests/__init__.py") == ("tests/",)
    assert _select(tmp_path, "tests/conftest.py") == ("tests/",)


def test_root_test_file_rename_reaches_the_selector_as_its_old_name() -> None:
    """git reports a rename as the new name alone unless rename detection is off, and the selector
    needs the old name to find the tests that still import it."""
    workflow = (
        Path(__file__).parents[3] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    lines = [ln for ln in workflow.splitlines() if "> changed.txt" in ln]
    assert len(lines) == 2
    assert all("--no-renames" in ln for ln in lines)
