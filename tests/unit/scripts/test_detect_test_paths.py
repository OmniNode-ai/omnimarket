# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket change-aware test path resolution."""

from __future__ import annotations

import os
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

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


def test_nonexistent_test_dirs_filtered_out(tmp_path: Path) -> None:
    """Mapped test dirs that do not exist on disk must be dropped.

    Why: adapter source changes map to tests/adapters/, but adapter tests
    currently live as flat tests/test_adapter_*.py files. Passing a missing
    directory to pytest exits with collection error 4.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "inference").mkdir()
    paths = resolve_test_paths(
        ["src/omnimarket/adapters/codex/runtime_client.py"],
        ADJACENCY_PATH,
        repo_root=tmp_path,
    )
    assert "tests/adapters/" not in paths
    assert all((tmp_path / p).exists() for p in paths)


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
        changed_files=["src/omnimarket/intelligence/client.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None
    assert sel.split_count >= 1
    assert len(sel.matrix) == sel.split_count


def test_unknown_src_module_escalates_to_full_suite() -> None:
    sel = compute_selection(
        changed_files=["src/omnimarket/unknown_module/foo.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.UNMAPPED_MODULE
    assert sel.selected_paths == ["tests/"]


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
        changed_files=["src/omnimarket/intelligence/client.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert len(sel.matrix) == sel.split_count
    assert sel.matrix == list(range(1, sel.split_count + 1))


# --- OMN-15277: root-level changed test files must never be dropped ---
#
# `tests/` holds ~435 root-level `*.py` modules (flat layout, not
# `tests/<module>/`). `_resolve()`'s test-prefix branch unconditionally built
# `tests/<parts[1]>/` for every changed tests/ path; for a root-level file
# `parts[1]` is the filename itself, so the constructed string is a
# directory-shaped pseudo-path (`tests/<file>.py/`), not a real directory.
#
# Two distinct on-disk outcomes, both wrong, neither load-bearing correctness:
#   * File absent at selection time (e.g. deleted in the diff, or -- as
#     reproduced below with a synthetic filename matching the ticket's
#     illustrative example -- simply doesn't exist): the on-disk `.exists()`
#     filter drops the pseudo-directory silently. If another selected_paths
#     entry already exists (e.g. from a co-occurring src change), the
#     `if not selected: selected = ["tests/"]` blanket fallback never fires
#     either, so the file is unrepresented in `selected_paths` with zero
#     attribution.
#   * File present at selection time (the common add/modify case, verified
#     directly against this repo's real fixtures on .200): `.exists()` passes
#     only because `pathlib.Path` silently strips the trailing slash when
#     parsing path components, so `tests/<file>.py/` and `tests/<file>.py`
#     resolve to the same filesystem entry. pytest happens to tolerate this
#     malformed arg (verified with `--collect-only` and with
#     `--splits/--group`) -- but that tolerance is an accidental pathlib/
#     pytest normalization, not a designed contract, and the emitted
#     `TestPath` value is file-shaped rather than the directory-only shape
#     the model documents.
#
# The fix (CHANGED_TEST_UNNARROWABLE) makes both outcomes into one
# deterministic, attributable full-suite escalation instead of depending on
# filesystem state at selection time.


def test_root_level_test_file_alone_is_covered_pre_and_post_fix() -> None:
    """Baseline: a lone root-level test change already falls to `tests/`.

    This passes both before and after the fix (the accidental-safety case
    from the ticket) -- included so the dangerous case below is contrasted
    against a case that was never broken.
    """
    sel = compute_selection(
        changed_files=["tests/test_agent_registry.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert "tests/test_agent_registry.py" not in [
        p for p in sel.selected_paths if p.endswith(".py")
    ]  # never selected as a literal file path in this repo's shape
    assert sel.selected_paths == ["tests/"] or sel.is_full_suite is True


def test_root_level_test_file_with_concurrent_src_change_is_never_dropped() -> None:
    """OMN-15277 RED case: co-occurring src change must not silently drop the
    changed root-level test file.

    Pre-fix this returns is_full_suite=False, selected_paths=['tests/nodes/']
    -- the changed tests/test_agent_registry.py contributes nothing and is
    silently uncollected despite the diff touching it directly.
    """
    sel = compute_selection(
        changed_files=[
            "tests/test_agent_registry.py",
            "src/omnimarket/nodes/node_dispatch_worker/handler.py",
        ],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    covered = sel.is_full_suite or any(
        "tests/test_agent_registry.py".startswith(p.rstrip("/") + "/")
        or p == "tests/"
        or p == "tests/test_agent_registry.py"
        for p in sel.selected_paths
    )
    assert covered, (
        f"changed root-level test file dropped from selection: {sel.selected_paths!r}"
    )


def test_root_level_test_file_escalates_with_named_reason() -> None:
    """The escalation must be attributable, not a bare fallback re-use."""
    sel = compute_selection(
        changed_files=[
            "tests/test_agent_registry.py",
            "src/omnimarket/nodes/node_dispatch_worker/handler.py",
        ],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.CHANGED_TEST_UNNARROWABLE


def test_root_level_non_test_py_file_also_escalates() -> None:
    """A root-level helper module (not test_*.py, e.g. constants.py) is just
    as capable of silently breaking every consumer as a test_*.py file --
    the fix must not special-case the `test_` filename prefix.
    """
    sel = compute_selection(
        changed_files=[
            "tests/constants.py",
            "src/omnimarket/nodes/node_dispatch_worker/handler.py",
        ],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.CHANGED_TEST_UNNARROWABLE


def test_subdirectory_test_file_change_still_narrows_no_regression() -> None:
    """No-regression: a changed test file inside a real subdirectory keeps
    narrowing to that directory -- it must NOT escalate to full suite.
    """
    sel = compute_selection(
        changed_files=["tests/inference/test_something.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False
    assert "tests/inference/" in sel.selected_paths


def test_pure_src_diff_narrowing_unchanged_no_regression() -> None:
    """No-regression: a diff touching no tests/ path at all is unaffected by
    the new escalation path.
    """
    sel = compute_selection(
        changed_files=["src/omnimarket/intelligence/client.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None


def test_conftest_change_keeps_its_own_reason_no_regression() -> None:
    """No-regression: tests/conftest.py is both root-level AND a declared
    test_infrastructure_path -- it must keep reporting TEST_INFRASTRUCTURE,
    not get reclassified as CHANGED_TEST_UNNARROWABLE. Reason attribution is
    part of the observable contract (dashboards/alerts key off it).
    """
    sel = compute_selection(
        changed_files=["tests/conftest.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.TEST_INFRASTRUCTURE


def test_resolve_test_paths_never_emits_nonexistent_root_pseudo_dir() -> None:
    """Direct resolve_test_paths() call: the bogus tests/<file>.py/ pseudo-dir
    must never leak into the returned path list (it never existed on disk).
    """
    paths = resolve_test_paths(
        ["tests/test_agent_registry.py"],
        ADJACENCY_PATH,
    )
    assert "tests/test_agent_registry.py/" not in paths


def test_resolve_test_paths_direct_api_handles_an_existing_root_file_too(
    tmp_path: Path,
) -> None:
    """CodeRabbit-flagged gap: the synthetic-nonexistent-file test above only
    proves the "file absent" half of the defect for the direct
    resolve_test_paths() API. compute_selection()'s CHANGED_TEST_UNNARROWABLE
    check never reaches _resolve() for a root-level test path (it
    short-circuits first), so a caller of resolve_test_paths() directly
    still needs _resolve() itself to never emit the malformed
    tests/<file>.py/ shape for a file that DOES exist on disk.
    """
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    real_root_file = tests_dir / "test_something_real.py"
    real_root_file.write_text("def test_x() -> None:\n    assert True\n")

    paths = resolve_test_paths(
        ["tests/test_something_real.py"],
        ADJACENCY_PATH,
        repo_root=tmp_path,
    )
    assert not any(p.endswith(".py/") for p in paths)
    assert "tests/test_something_real.py/" not in paths
    # The real containing scope (tests/ itself) must be returned instead of
    # nothing and instead of a malformed per-file path.
    assert paths == ["tests/"]


def test_real_existing_root_test_file_also_escalates_not_malformed_path() -> None:
    """A REAL, on-disk root-level test file (not the synthetic nonexistent
    fixture used above) must escalate the same way.

    Pre-fix, this repo's own real fixture (tests/test_actual_cost_recompute_
    omn13355.py) survived only because pathlib silently strips the trailing
    slash from `tests/<file>.py/`, so `.exists()` happened to pass and pytest
    happened to tolerate the malformed arg -- an accidental pathlib/pytest
    normalization, not a designed contract (verified directly: pre-fix,
    selected_paths contained the literal malformed string
    'tests/test_actual_cost_recompute_omn13355.py/'). Post-fix this must be a
    deterministic, attributable full-suite escalation instead, so correctness
    no longer depends on that accident.
    """
    real_file = "tests/test_actual_cost_recompute_omn13355.py"
    assert (Path(__file__).parents[3] / real_file).is_file(), (
        "fixture file moved/renamed -- update this test's real-file target"
    )
    sel = compute_selection(
        changed_files=[
            real_file,
            "src/omnimarket/nodes/node_dispatch_worker/handler.py",
        ],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.CHANGED_TEST_UNNARROWABLE
    assert not any(p.endswith(".py/") for p in sel.selected_paths)


def test_contract_change_still_selects_the_repo_wide_gate_dir() -> None:
    """OMN-15639: narrowing must never drop tests/gates/.

    The consumer-group declaration gate walks every ``src/**/contract.yaml``.
    Without the ``always_selected_paths`` union, changing one node's
    ``contract.yaml`` resolves to module ``nodes`` and selects only
    ``tests/nodes/`` -- the gate would not run, so a re-added
    ``event_bus.consumer_group`` would merge green on the everyday dev path.
    This is the RED that wiring closes.
    """
    sel = compute_selection(
        changed_files=[
            "src/omnimarket/nodes/node_dispatch_worker/contract.yaml",
        ],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False, (
        "fixture drifted -- this test must exercise the NARROWED path, "
        "otherwise it passes vacuously via full-suite escalation"
    )
    assert "tests/gates/" in sel.selected_paths, (
        "tests/gates/ was narrowed away by a contract-only change; the "
        "repo-wide consumer-group gate is bypassable on dev PRs. "
        f"selected={sel.selected_paths}"
    )


def test_always_selected_paths_are_not_dropped_when_missing_on_disk() -> None:
    """A missing gate dir must reach pytest and fail loudly, not vanish.

    ``_resolve()`` filters mapped paths against the on-disk tree. If
    ``always_selected_paths`` went through that filter, deleting
    ``tests/gates/`` would silently remove the gate from every narrowed run
    instead of erroring -- the exact vacuous-green shape the gate exists to
    prevent.
    """
    from scripts.ci.test_selection_loader import load_adjacency_map

    config = load_adjacency_map(ADJACENCY_PATH)
    assert config.always_selected_paths, (
        "always_selected_paths is empty -- the repo-wide gate union is inert"
    )
    for path in config.always_selected_paths:
        assert path.startswith("tests/"), f"{path} is not under tests/"
        assert path.endswith("/"), f"{path} must be written as a directory"


def test_always_selected_paths_not_unioned_into_full_tests_root() -> None:
    """``tests/`` already contains the gate dir -- do not append a redundant arg."""
    sel = compute_selection(
        changed_files=["src/omnimarket/not_a_known_module/thing.py"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is True
    assert sel.selected_paths == ["tests/"]


# OMN-20180: protected surfaces and unmapped source modules must fail closed.


@pytest.fixture(scope="module")
def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ADJACENCY_PATH.parents[2],
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\0").split("\0")


@pytest.mark.parametrize(
    "path",
    [
        "src/omnimarket/delegation/__init__.py",
        "src/omnimarket/delegated_test_loop/__init__.py",
        "src/omnimarket/inference/__init__.py",
        "src/omnimarket/cost/__init__.py",
        "src/omnimarket/configs/routing_tiers.yaml",
        "src/omnimarket/configs/bifrost_delegation.yaml",
        "src/omnimarket/nodes/node_delegation_routing_reducer/handlers/handler_delegation_routing.py",
        "src/omnimarket/nodes/node_delegation_quality_gate_reducer/contract.yaml",
        "src/omnimarket/nodes/node_model_router/handlers/handler_model_router.py",
        "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml",
        "src/omnimarket/nodes/node_ab_compare_orchestrator/contract.yaml",
        "src/omnimarket/nodes/node_judge_verdict_parse_compute/contract.yaml",
        "src/omnimarket/nodes/node_projection_savings/contract.yaml",
        "src/omnimarket/nodes/node_projection_llm_cost/contract.yaml",
        "src/omnimarket/nodes/node_routing_policy_engine/contract.yaml",
        "src/omnimarket/nodes/node_house_routing_overlay_effect/contract.yaml",
        "src/omnimarket/nodes/node_projection_delegation/contract.yaml",
        "src/omnimarket/nodes/node_pr_delegated_fix_effect/contract.yaml",
        "src/omnimarket/nodes/node_adr_segmentation_llm_effect/contract.yaml",
        "src/omnimarket/nodes/node_pr_semantic_grader_llm_effect/contract.yaml",
    ],
)
def test_protected_surface_escalates(path: str, tracked_paths: list[str]) -> None:
    assert path in tracked_paths, "fixture must reference a real tracked file"
    sel = compute_selection([path], ADJACENCY_PATH, ref_name="jonah/feature")
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.PROTECTED_SURFACE
    assert sel.selected_paths == ["tests/"]
    assert sel.split_count == 20


def test_non_delegation_node_still_narrows(tracked_paths: list[str]) -> None:
    path = "src/omnimarket/nodes/node_code_embedding_effect/handlers/handler_code_embedding_effect.py"
    assert path in tracked_paths
    sel = compute_selection([path], ADJACENCY_PATH, ref_name="jonah/feature")
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None
    assert sel.selected_paths == ["tests/gates/", "tests/nodes/"]


@pytest.mark.parametrize(
    "path",
    [
        "src/omnimarket/cloud/completion_bound.py",
        "src/omnimarket/codegen/models.py",
        "src/omnimarket/__init__.py",
    ],
)
def test_unmapped_source_with_mapped_module_escalates(
    path: str, tracked_paths: list[str]
) -> None:
    assert path in tracked_paths
    sel = compute_selection(
        [path, "src/omnimarket/nodes/node_code_embedding_effect/contract.yaml"],
        ADJACENCY_PATH,
        ref_name="jonah/feature",
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.UNMAPPED_MODULE
    assert sel.selected_paths == ["tests/"]


@pytest.mark.parametrize(
    ("ref_name", "event_name", "feature_flag_enabled", "extra_paths", "reason"),
    [
        ("main", "push", True, [], EnumFullSuiteReason.MAIN_BRANCH),
        ("feature", "merge_group", True, [], EnumFullSuiteReason.MERGE_GROUP),
        ("feature", "schedule", True, [], EnumFullSuiteReason.SCHEDULED),
        ("feature", "pull_request", False, [], EnumFullSuiteReason.FEATURE_FLAG_OFF),
        (
            "feature",
            "pull_request",
            True,
            ["tests/conftest.py"],
            EnumFullSuiteReason.TEST_INFRASTRUCTURE,
        ),
    ],
)
def test_existing_full_suite_checks_precede_protected_and_unmapped_checks(
    ref_name: str,
    event_name: str,
    feature_flag_enabled: bool,
    extra_paths: list[str],
    reason: EnumFullSuiteReason,
) -> None:
    sel = compute_selection(
        [
            "src/omnimarket/nodes/node_delegation_routing_reducer/contract.yaml",
            "src/omnimarket/cloud/completion_bound.py",
            *extra_paths,
        ],
        ADJACENCY_PATH,
        ref_name=ref_name,
        event_name=event_name,
        feature_flag_enabled=feature_flag_enabled,
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == reason


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/delegation/test_something.py",
        "tests/delegation_golden/test_something.py",
    ],
)
def test_delegation_test_directory_change_still_narrows(path: str) -> None:
    sel = compute_selection([path], ADJACENCY_PATH, ref_name="jonah/feature")
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None
    assert path.startswith(tuple(sel.selected_paths))


def _assert_globs_match_tracked_paths(globs: list[str], paths: list[str]) -> None:
    unmatched = [glob for glob in globs if not any(fnmatchcase(p, glob) for p in paths)]
    assert not unmatched, f"protection globs match no tracked paths: {unmatched}"


def test_every_protection_glob_matches_a_tracked_path(tracked_paths: list[str]) -> None:
    from scripts.ci.test_selection_loader import load_adjacency_map

    config = load_adjacency_map(ADJACENCY_PATH)
    assert config.full_suite_path_globs, (
        "protected surface configuration must not be empty"
    )
    _assert_globs_match_tracked_paths(config.full_suite_path_globs, tracked_paths)


def test_typo_in_protection_glob_is_caught(tracked_paths: list[str]) -> None:
    with pytest.raises(AssertionError, match="protection globs match no tracked paths"):
        _assert_globs_match_tracked_paths(
            ["src/omnimarket/nodes/node_delegtion_*/**"], tracked_paths
        )


def test_protection_globs_default_to_empty_list() -> None:
    from scripts.ci.test_selection_loader import ModelAdjacencyMap, load_adjacency_map

    raw = load_adjacency_map(ADJACENCY_PATH).model_dump()
    raw.pop("full_suite_path_globs")
    assert ModelAdjacencyMap.model_validate(raw).full_suite_path_globs == []


@pytest.mark.parametrize("invalid", ["src/**", ("src/**",), [123], [None]])
def test_protection_globs_reject_invalid_types(invalid: object) -> None:
    from pydantic import ValidationError

    from scripts.ci.test_selection_loader import ModelAdjacencyMap, load_adjacency_map

    raw = load_adjacency_map(ADJACENCY_PATH).model_dump()
    raw["full_suite_path_globs"] = invalid
    with pytest.raises(ValidationError):
        ModelAdjacencyMap.model_validate(raw)


# --- OMN-20305: an empty selection must not become one unsplit tests/ job ---
#
# omnimarket#3175 (a workflow-only PR) selected nothing, fell back to
# ["tests/"] and emitted is_full_suite=False with split_count=1, so the whole
# suite ran serially as `Tests (Split 1/1)` for 63 minutes. The fallback is a
# full-suite run and must use the same 20-shard matrix as every other one.


def test_empty_selection_fallback_uses_full_suite_shard_matrix() -> None:
    sel = compute_selection(
        changed_files=[".github/workflows/some-other.yml"],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.selected_paths == ["tests/"]
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.NO_NARROWABLE_SELECTION
    assert sel.split_count == 20
    assert sel.matrix == list(range(1, 21))


def test_workflow_only_diff_cli_emits_twenty_splits(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text(".github/workflows/ci.yml\n")
    out = tmp_path / "out.json"
    import contextlib
    import io
    import json

    from scripts.ci.detect_test_paths import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(
            [
                "--changed-files-from",
                str(changed),
                "--ref-name",
                "jonah/feature",
                "--event-name",
                "pull_request",
            ]
        )
    out.write_text(buf.getvalue())
    data = json.loads(out.read_text())
    assert rc == 0
    assert data["is_full_suite"] is True
    assert data["split_count"] == 20
    assert len(data["matrix"]) == 20
