# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket change-aware test path resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.adapter_test_selection_policy_file import (
    load_policy,
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
    REPO_ROOT,
    compute_selection,
    resolve_test_paths,
)
from scripts.ci.test_selection_models import (  # noqa: E402
    EnumFullSuiteReason,
    ModelTestSelection,
)

_REMOTE_LANE_HANDLER = (
    "src/omnimarket/nodes/node_remote_lane_compute/handlers/"
    "handler_remote_lane_placement.py"
)


def test_adjacency_yaml_loads() -> None:
    from scripts.ci.test_selection_loader import load_adjacency_map

    config = load_adjacency_map(ADJACENCY_PATH)
    assert config.schema_version == 1
    assert "models" in config.shared_modules
    assert "nodes" in config.adjacency


def test_src_change_maps_to_test_subdir() -> None:
    # OMN-23719: a node change maps to that node's test directory, not tests/nodes/.
    paths = resolve_test_paths(
        [_REMOTE_LANE_HANDLER],
        ADJACENCY_PATH,
    )
    assert "tests/nodes/node_remote_lane_compute/" in paths
    assert "tests/nodes/" not in paths


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
    # OMN-23719: node_dispatch_worker reaches tests/conftest.py through the import
    # graph, so its change now takes the full suite; this node's change narrows.
    sel = compute_selection(
        changed_files=[
            "src/omnimarket/nodes/node_remote_lane_compute/contract.yaml",
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


def test_node_change_still_selects_the_cross_node_reach_in_test() -> None:
    """OMN-17232: a one-node change must still run the repo-wide reach-in test.

    omnimarket#4581 changed ``node_remote_lane_compute`` only, so the narrowed
    selection ran that node's tests and not ``tests/test_no_cross_node_reach_in.py``.
    The PR was green, the dev push skips the suite (OMN-19998), and the new
    cross-node model import failed every PR synced with dev afterwards.
    """
    sel = compute_selection(
        changed_files=[
            "src/omnimarket/nodes/node_remote_lane_compute/handlers/handler_remote_lane_placement.py",
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
    assert "tests/test_no_cross_node_reach_in.py" in sel.selected_paths, (
        f"the reach-in test was narrowed away. selected={sel.selected_paths}"
    )
    assert (
        ADJACENCY_PATH.parents[2] / "tests/test_no_cross_node_reach_in.py"
    ).is_file()


def test_node_change_still_selects_the_contract_binding_test() -> None:
    """OMN-23649: the repo-wide contract binding test reads every
    contracts/OMN-*.yaml; a change that adds one contract narrowed to its own
    node's tests and merged a contract that test refuses, so dev went red on
    every open change.
    """
    sel = compute_selection(
        changed_files=[
            "src/omnimarket/nodes/node_remote_lane_compute/handlers/handler_remote_lane_placement.py",
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
    assert "tests/ci/test_repo_evidence_caller_workflow.py" in sel.selected_paths, (
        f"the contract binding test was narrowed away. selected={sel.selected_paths}"
    )
    assert (
        ADJACENCY_PATH.parents[2] / "tests/ci/test_repo_evidence_caller_workflow.py"
    ).is_file()


@pytest.mark.parametrize(
    "changed",
    [
        "src/omnimarket/nodes/node_remote_lane_compute/handlers/handler_remote_lane_placement.py",
        "src/omnimarket/nodes/node_remote_lane_compute/contract.yaml",
    ],
)
def test_always_selected_paths_keep_binding_and_dogfood_tests_on_narrowed_selection(
    changed: str,
) -> None:
    """OMN-23649: a one-node change still selects the repo-wide contract binding
    test and the market node runtime dogfood test.

    The first fix left the dogfood test out because the narrowed selection then
    exceeded the shard weight bound and ran the full suite. OMN-23719 narrowed a
    node change to its own tests, so the pair fits: this asserts the selection
    stays narrowed (not the full suite) and carries both tests.
    """
    sel = compute_selection(
        changed_files=[changed],
        adjacency_path=ADJACENCY_PATH,
        ref_name="jonah/feature",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    assert sel.is_full_suite is False, (
        "fixture drifted -- this test must exercise the NARROWED path, "
        "otherwise it passes vacuously via full-suite escalation"
    )
    for always in (
        "tests/ci/test_repo_evidence_caller_workflow.py",
        "tests/test_market_node_runtime_dogfood.py",
    ):
        assert always in sel.selected_paths, (
            f"{always} was narrowed away. selected={sel.selected_paths}"
        )
        assert (ADJACENCY_PATH.parents[2] / always).is_file()


def test_always_selected_paths_reject_a_non_test_entry() -> None:
    from scripts.ci.test_selection_loader import ModelAdjacencyMap, load_adjacency_map

    base = load_adjacency_map(ADJACENCY_PATH).model_dump()
    for bad in ("tests/gates", "src/omnimarket/models/", "scripts/ci/helper.py"):
        with pytest.raises(ValueError, match="always_selected_paths"):
            ModelAdjacencyMap.model_validate({**base, "always_selected_paths": [bad]})


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
        assert path.endswith(("/", ".py")), (
            f"{path} must be a directory with a trailing slash or a .py test file"
        )


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
    # OMN-23719: the node's own tests and the repo-wide gates, not all of tests/nodes/.
    assert "tests/nodes/node_code_embedding_effect/" in sel.selected_paths
    assert "tests/nodes/" not in sel.selected_paths
    for always in (
        "tests/ci/test_repo_evidence_caller_workflow.py",
        "tests/gates/",
        "tests/test_no_cross_node_reach_in.py",
        "tests/test_market_node_runtime_dogfood.py",
    ):
        assert always in sel.selected_paths


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


def test_delegation_test_directory_change_still_narrows() -> None:
    path = "tests/delegation_golden/test_something.py"
    sel = compute_selection([path], ADJACENCY_PATH, ref_name="jonah/feature")
    assert sel.is_full_suite is False
    assert sel.full_suite_reason is None
    assert path.startswith(tuple(sel.selected_paths))


def test_delegation_unit_test_change_is_not_a_protected_surface() -> None:
    # tests/unit/ is too heavy to narrow (OMN-22868), so this change takes the
    # full-suite matrix for its weight, not as a protected surface.
    sel = compute_selection(
        ["tests/unit/delegation/test_something.py"],
        ADJACENCY_PATH,
        ref_name="jonah/feature",
    )
    assert sel.is_full_suite is True
    assert sel.full_suite_reason == EnumFullSuiteReason.NARROWED_SELECTION_TOO_HEAVY


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
        changed_files=["README.md"],
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


_HEAVY_CHANGE = [
    "src/omnimarket/nodes/node_canonical_clone_dirty_converge_effect/contract.yaml",
    "tests/unit/clone_dirty_converge/test_canonical_clone_dirty_converge.py",
]
_LIGHT_CHANGE = [
    "src/omnimarket/nodes/node_canonical_clone_dirty_converge_effect/contract.yaml",
]


def test_narrowed_selection_with_the_unit_tree_takes_the_full_suite_matrix() -> None:
    selection = compute_selection(
        _HEAVY_CHANGE, ADJACENCY_PATH, ref_name="omn-22868-x", event_name="pull_request"
    )
    assert selection.is_full_suite is True
    assert (
        selection.full_suite_reason == EnumFullSuiteReason.NARROWED_SELECTION_TOO_HEAVY
    )
    assert selection.split_count == 20
    assert selection.selected_paths == ["tests/"]


def test_narrowed_selection_under_the_bound_stays_narrowed() -> None:
    selection = compute_selection(
        _LIGHT_CHANGE, ADJACENCY_PATH, ref_name="omn-22868-x", event_name="pull_request"
    )
    assert selection.is_full_suite is False
    assert "tests/unit/" not in selection.selected_paths
    # The recorded durations move with every refresh; the bound is the claim.
    assert 3 <= selection.split_count <= 5


def test_without_the_selector_the_heavy_selection_is_two_unplanned_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Failing control (OMN-23296): a policy that sizes by path count alone, as the
    # old script did, gives the selection with tests/unit/ only the path-count
    # rule's shards, each carrying past the target. (Before OMN-23719 narrowed
    # node changes, this was tests/nodes/ with tests/unit/: two shards of about
    # 2,000 recorded seconds each.)
    from omnimarket.nodes.node_test_selection_selector_compute.handlers import (
        handler_test_selection_selector as selector,
    )

    loose = load_policy().model_copy(
        update={"target_shard_seconds": 10**9, "max_narrowed_shards": 5}
    )
    monkeypatch.setattr(
        sys.modules["scripts.ci.detect_test_paths"],
        "HandlerTestSelectionSelector",
        lambda _source, _policy: selector.HandlerTestSelectionSelector(None, loose),
    )
    selection = compute_selection(
        _HEAVY_CHANGE, ADJACENCY_PATH, ref_name="omn-22868-x", event_name="pull_request"
    )
    assert selection.is_full_suite is False
    assert selection.split_count == loose.path_count_max_shards
    assert "tests/unit/" in selection.selected_paths
    assert _shard_seconds(selection) > load_policy().target_shard_seconds


def _shard_seconds(selection: ModelTestSelection) -> float:
    """Recorded seconds one shard of a narrowed selection carries, read from the durations file."""
    durations = json.loads((REPO_ROOT / "config/test_file_durations.json").read_text())
    covered = sum(
        seconds
        for test_file, seconds in durations.items()
        if any(
            test_file == path or (path.endswith("/") and test_file.startswith(path))
            for path in selection.selected_paths
        )
    )
    return covered / selection.split_count


def _every_single_module_change() -> list[list[str]]:
    adjacency = yaml.safe_load(ADJACENCY_PATH.read_text())["adjacency"]
    return [
        [f"src/omnimarket/{module}/x.py", extra]
        for module in sorted(adjacency)
        for extra in ("tests/unit/test_x.py", "tests/nodes/test_x.py", "docs/x.md")
    ]


def test_split_count_by_duration_no_narrowed_shard_past_target() -> None:
    target = load_policy().target_shard_seconds
    narrowed = 0
    for changed in [_HEAVY_CHANGE, _LIGHT_CHANGE, *_every_single_module_change()]:
        selection = compute_selection(
            changed, ADJACENCY_PATH, ref_name="omn-23296-x", event_name="pull_request"
        )
        if selection.is_full_suite:
            assert selection.split_count == 20
            continue
        narrowed += 1
        assert _shard_seconds(selection) <= target, (changed, selection)
    assert narrowed > 0


def test_shards_fit_target_heavy_selection_is_the_planned_matrix() -> None:
    selection = compute_selection(
        _HEAVY_CHANGE, ADJACENCY_PATH, ref_name="omn-23296-x", event_name="pull_request"
    )
    assert selection.is_full_suite is True
    assert selection.split_count == 20
    assert selection.matrix == list(range(1, 21))


def test_split_count_by_duration_mid_weight_gets_more_shards_than_paths() -> None:
    # One mid-weight path, at least three shards. The test is given its own
    # durations record so its claim does not move with the real one.
    from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
        HandlerTestSelectionSelector,
    )
    from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
        ModelSelectorRequest,
    )

    policy = load_policy()

    class _FixedDurations:
        def __init__(self, durations: dict[str, float]) -> None:
            self._durations = durations

        def read(self, repo_root: str, durations_path: str) -> dict[str, float] | None:
            return self._durations

    estimate = 3.5 * policy.target_shard_seconds
    durations = {f"tests/unit/nodes/node_x/test_{i}.py": estimate / 7 for i in range(7)}
    decision = HandlerTestSelectionSelector(_FixedDurations(durations), policy).handle(
        ModelSelectorRequest(
            selected_paths=("tests/unit/nodes/",), repo_root=str(REPO_ROOT)
        )
    )
    assert decision.split_count is not None
    assert 3 <= decision.split_count <= 5
    assert decision.shard_seconds is not None
    assert decision.shard_seconds <= policy.target_shard_seconds


def test_a_bounded_selection_with_no_durations_file_fails_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests" / "nodes").mkdir(parents=True)
    selection = compute_selection(
        _LIGHT_CHANGE,
        ADJACENCY_PATH,
        ref_name="omn-22868-x",
        event_name="pull_request",
        repo_root=tmp_path,
    )
    assert selection.is_full_suite is True
    assert (
        selection.full_suite_reason == EnumFullSuiteReason.NARROWED_SELECTION_TOO_HEAVY
    )


# OMN-23719: node-scoped narrowing over the import graph. On dev the whole of
# tests/nodes/ summed to 1800.75 recorded seconds against the selector's 1800 s
# bound, so every node change took the full suite. These run on a small tree so
# the claim does not move with the real repository.


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    durations = {rel: 1.0 for rel in files if rel.startswith("tests/")}
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "test_file_durations.json").write_text(json.dumps(durations))
    return root


_NODES = {
    "src/omnimarket/__init__.py": "",
    "src/omnimarket/nodes/__init__.py": "",
    "src/omnimarket/nodes/node_a/__init__.py": "",
    "src/omnimarket/nodes/node_a/handler.py": "VALUE = 1\n",
    "src/omnimarket/nodes/node_b/__init__.py": "",
    "src/omnimarket/nodes/node_b/handler.py": (
        "from omnimarket.nodes.node_a.handler import VALUE\n"
    ),
    "src/omnimarket/nodes/node_c/__init__.py": "",
    "src/omnimarket/nodes/node_c/handler.py": "OTHER = 2\n",
    "src/omnimarket/handlers/__init__.py": "",
    "src/omnimarket/handlers/h.py": "from omnimarket.nodes.node_a import handler\n",
    "src/omnimarket/handlers/g.py": "from .h import handler\n",
    "src/omnimarket/nodes/node_e/__init__.py": "",
    "src/omnimarket/nodes/node_e/handler.py": "from omnimarket.handlers import g\n",
    "src/omnimarket/nodes/node_d/__init__.py": "",
    "src/omnimarket/nodes/node_d/contract.yaml": "dependencies:\n  - node_a\n",
    "src/omnimarket/nodes/node_lonely/__init__.py": "",
    "src/omnimarket/nodes/node_lonely/handler.py": "X = 0\n",
    "tests/__init__.py": "",
    "tests/nodes/__init__.py": "",
    "tests/nodes/test_flat_walker.py": "def test_walk():\n    pass\n",
    "tests/nodes/node_a/__init__.py": "",
    "tests/nodes/node_a/test_a.py": "from omnimarket.nodes.node_a.handler import VALUE\n",
    "tests/nodes/node_b/__init__.py": "",
    "tests/nodes/node_b/test_b.py": "def test_b():\n    pass\n",
    "tests/nodes/node_c/__init__.py": "",
    "tests/nodes/node_c/test_c.py": "def test_c():\n    pass\n",
    "tests/nodes/node_d/__init__.py": "",
    "tests/nodes/node_d/test_d.py": "def test_d():\n    pass\n",
    "tests/nodes/node_e/__init__.py": "",
    "tests/nodes/node_e/test_e.py": "def test_e():\n    pass\n",
    "tests/nodes/node_c/support.py": "from omnimarket.nodes.node_a import handler\n",
    "tests/nodes/node_c/test_uses_support.py": (
        "from tests.nodes.node_c.support import handler\n"
    ),
    "tests/other/__init__.py": "",
    "tests/other/test_names_a.py": "TARGET = 'omnimarket.nodes.node_a.handler.VALUE'\n",
    "tests/other/test_unrelated.py": "def test_x():\n    pass\n",
}


def _select(root: Path, changed: list[str]) -> ModelTestSelection:
    return compute_selection(
        changed, ADJACENCY_PATH, ref_name="omn-23719-x", repo_root=root
    )


def test_node_change_selects_its_dependents_not_the_whole_node_tree(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path, _NODES)
    sel = _select(root, ["src/omnimarket/nodes/node_a/handler.py"])
    assert sel.is_full_suite is False
    paths = sel.selected_paths
    assert "tests/nodes/" not in paths
    assert "tests/nodes/node_a/" in paths  # its own tests
    assert "tests/nodes/node_b/" in paths  # node_b imports node_a
    assert (
        "tests/nodes/node_e/" in paths
    )  # node_e -> handlers.g -> handlers.h -> node_a
    assert "tests/nodes/node_d/" in paths  # node_d's contract names node_a
    assert "tests/other/test_names_a.py" in paths  # spells node_a in a string
    assert "tests/nodes/node_c/test_uses_support.py" in paths  # through a helper
    assert "tests/nodes/test_flat_walker.py" in paths  # flat tests/nodes/ files
    assert "tests/nodes/node_c/" not in paths
    assert "tests/other/test_unrelated.py" not in paths
    assert "tests/gates/" in paths  # always-selected gates still unioned


def test_unrelated_node_change_does_not_select_the_dependents_of_another(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path, _NODES)
    sel = _select(root, ["src/omnimarket/nodes/node_c/handler.py"])
    assert sel.is_full_suite is False
    assert "tests/nodes/node_c/" in sel.selected_paths
    assert "tests/nodes/node_a/" not in sel.selected_paths
    assert "tests/nodes/node_b/" not in sel.selected_paths


def test_conftest_that_imports_the_node_selects_its_directory(tmp_path: Path) -> None:
    files = dict(_NODES)
    files["tests/other/conftest.py"] = "from omnimarket.nodes.node_a import handler\n"
    root = _tree(tmp_path, files)
    sel = _select(root, ["src/omnimarket/nodes/node_a/handler.py"])
    assert "tests/other/" in sel.selected_paths


def test_node_with_no_tests_falls_back_to_the_whole_node_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path, _NODES)
    sel = _select(root, ["src/omnimarket/nodes/node_lonely/handler.py"])
    assert "tests/nodes/" in sel.selected_paths


def test_deleted_node_falls_back_to_the_whole_node_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path, _NODES)
    sel = _select(root, ["src/omnimarket/nodes/node_gone/handler.py"])
    assert "tests/nodes/" in sel.selected_paths


def test_deleted_non_node_module_seeds_its_package(tmp_path: Path) -> None:
    files = dict(_NODES)
    # intelligence maps to reverse_deps [nodes] in the real adjacency map.
    files["src/omnimarket/intelligence/__init__.py"] = ""
    files["src/omnimarket/nodes/node_c/handler.py"] = (
        "from omnimarket.intelligence.removed import thing\n"
    )
    root = _tree(tmp_path, files)
    sel = _select(root, ["src/omnimarket/intelligence/removed.py"])
    assert sel.is_full_suite is False
    assert "tests/nodes/node_c/" in sel.selected_paths


def test_changed_node_test_narrows_to_its_directory_and_importers(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path, _NODES)
    sel = _select(root, ["tests/nodes/node_c/support.py"])
    assert sel.is_full_suite is False
    assert "tests/nodes/node_c/" in sel.selected_paths
    assert "tests/nodes/" not in sel.selected_paths
    assert "tests/nodes/node_a/" not in sel.selected_paths


def test_node_scoped_modules_must_be_mapped() -> None:
    from scripts.ci.test_selection_loader import ModelAdjacencyMap

    raw = yaml.safe_load(ADJACENCY_PATH.read_text())
    raw["node_scoped_modules"] = ["not_a_module"]
    with pytest.raises(ValueError, match="node_scoped_modules"):
        ModelAdjacencyMap.model_validate(raw)


def test_real_node_change_narrows_under_the_bound() -> None:
    # The red this closes: on dev a one-node change summed tests/nodes/ (1800.75 s)
    # past the 1800 s bound and took the full-suite matrix.
    sel = compute_selection(
        [_REMOTE_LANE_HANDLER], ADJACENCY_PATH, ref_name="omn-23719-x"
    )
    assert sel.is_full_suite is False
    assert "tests/nodes/" not in sel.selected_paths
    assert _shard_seconds(sel) <= load_policy().target_shard_seconds


def test_changed_package_init_seeds_every_module_beneath_it(tmp_path: Path) -> None:
    # Importing omnimarket.intelligence.sub runs intelligence/__init__.py first, so
    # node_c depends on the package's __init__ although it never imports that name.
    files = dict(_NODES)
    files["src/omnimarket/intelligence/__init__.py"] = "SETTING = 1\n"
    files["src/omnimarket/intelligence/sub.py"] = "VALUE = 2\n"
    files["src/omnimarket/nodes/node_c/handler.py"] = (
        "from omnimarket.intelligence.sub import VALUE\n"
    )
    root = _tree(tmp_path, files)
    sel = _select(root, ["src/omnimarket/intelligence/__init__.py"])
    assert sel.is_full_suite is False
    assert "tests/nodes/node_c/" in sel.selected_paths
    assert "tests/nodes/node_a/" not in sel.selected_paths


def test_non_python_source_outside_a_node_falls_back_to_the_whole_node_tree(
    tmp_path: Path,
) -> None:
    files = dict(_NODES)
    files["src/omnimarket/intelligence/__init__.py"] = ""
    files["src/omnimarket/intelligence/table.json"] = "{}\n"
    root = _tree(tmp_path, files)
    sel = _select(root, ["src/omnimarket/intelligence/table.json"])
    assert "tests/nodes/" in sel.selected_paths
