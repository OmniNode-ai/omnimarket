# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket change-aware test path resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ADJACENCY_PATH = (
    Path(__file__).parents[3] / "scripts" / "ci" / "test_selection_adjacency.yaml"
)


@pytest.fixture(autouse=True)
def _scripts_on_path(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


from scripts.ci.detect_test_paths import (  # noqa: E402
    compute_selection,
    resolve_test_paths,
)
from scripts.ci.test_selection_models import EnumFullSuiteReason  # noqa: E402


def test_adjacency_yaml_loads() -> None:
    from scripts.ci.test_selection_loader import load_adjacency_map

    config = load_adjacency_map(ADJACENCY_PATH)
    assert config.schema_version == 1
    assert "models" in config.shared_modules
    assert "nodes" in config.adjacency


def test_src_change_maps_to_test_subdir() -> None:
    paths = resolve_test_paths(
        ["src/omnimarket/nodes/node_dispatch_worker/handler.py"],
        ADJACENCY_PATH,
    )
    assert "tests/nodes/" in paths


def test_test_file_change_included() -> None:
    paths = resolve_test_paths(
        ["tests/inference/test_something.py"],
        ADJACENCY_PATH,
    )
    assert "tests/inference/" in paths


def test_shared_module_triggers_full_suite() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/models/some_model.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.SHARED_MODULE


def test_main_branch_triggers_full_suite() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/nodes/node_foo/handler.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="main",
        event_name="push",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.MAIN_BRANCH


def test_merge_group_triggers_full_suite() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/nodes/node_foo/handler.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="merge_group",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.MERGE_GROUP


def test_feature_flag_off_triggers_full_suite() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/nodes/node_foo/handler.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=False,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.FEATURE_FLAG_OFF


def test_test_infrastructure_change_triggers_full_suite() -> None:
    sel = compute_selection(
        changed_files=["tests/conftest.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_smart_selection_non_shared_module() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/cli/commands.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None
    assert sel.split_count >= 1
    assert len(sel.matrix) == sel.split_count


def test_unknown_src_module_falls_back_to_full_tests() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/unknown_module/foo.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False
    assert "tests/" in sel.selected_paths


def test_cli_entrypoint_produces_json(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("src/omnimarket/cli/commands.py\n")
    from scripts.ci.detect_test_paths import main

    ret = main(
        [
            "--changed-files-from",
            str(changed),
            "--ref-name",
            "jonah/feature",
            "--event-name",
            "pull_request",
            "--adjacency",
            str(ADJACENCY_PATH),
            "--feature-flag",
            "on",
        ]
    )
    assert ret == 0


def test_matrix_length_matches_split_count() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/cli/commands.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert len(sel.matrix) == sel.split_count
    assert sel.matrix == list(range(1, sel.split_count + 1))
