# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the protected delegation-surface measurement receipt.

The adjacency file sends a change under a protected surface to the whole suite. The receipt in
``config/test_selection_protected_surface_receipt.json`` states what each protected merge of the replay window
would select under reverse import closure alone and under closure plus a fixed delegation test set, in test
seconds from the frozen durations, and compares both with the narrowed-run bound. These tests recompute every
figure from the frozen tree the replay input names, so a missing merge or a wrong figure fails here.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_tree_snapshot import (
    materialize_tree,
    read_snapshot_file,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers import (
    handler_test_path_resolution as resolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_protected_surface_receipt import (
    EnumProtectedEndState,
    ModelProtectedSurfaceReceipt,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelPathResolutionRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "config" / "test_selection_protected_surface_receipt.json"
BASELINE_PATH = REPO_ROOT / "config" / "test_selection_replay_receipt.json"
REPLAY_INPUT_PATH = "config/test_selection_replay_input.json"

#: Merges that changed a protected surface and that the measurement must state, by full sha.
NAMED_MERGES = (
    "360f20de1bb4ace5280ee420bfd29454b3b46bb5",
    "42a3a4de51f71cb2c30dab2e81cc127cc2b931b7",
    "9b0319b2638198df6d87c4386466bb34ccffba8f",
)
FIXED_DIRECTORIES = ("tests/delegation_golden/", "tests/unit/delegation/")
PROTECTED_REASON = "protected_surface"
WHOLE_SUITE = "tests/"


def canonical(document: Any) -> str:
    return json.dumps(document, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixed_set_paths(snapshot: ModelTreeSnapshot) -> tuple[str, ...]:
    """The two delegation test directories and every flat ``tests/*delegat*.py`` file of the frozen tree."""
    flat = sorted(
        path
        for path in snapshot.files
        if path.startswith("tests/")
        and path.count("/") == 1
        and path.endswith(".py")
        and "delegat" in path.rsplit("/", 1)[-1]
    )
    return (*FIXED_DIRECTORIES, *flat)


def _seconds(
    selector: HandlerTestSelectionSelector, root: Path, paths: tuple[str, ...]
) -> float:
    decision = selector.handle(
        ModelSelectorRequest(selected_paths=paths, repo_root=str(root))
    )
    assert decision.estimated_seconds is not None
    return round(decision.estimated_seconds, 6)


def measure_protected_surface(
    snapshot: ModelTreeSnapshot,
    replay_input: dict[str, Any],
    protected_in_window: tuple[str, ...],
    beside_window: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Recompute the receipt document from the frozen tree.

    ``protected_in_window`` are the protected merges found among the replay input's merges, whose changed files
    are read from the input; ``beside_window`` maps a merge measured beside them to its changed files.
    """
    window = {m["sha"]: tuple(m["changed_files"]) for m in replay_input["merges"]}
    merges: list[tuple[str, bool, tuple[str, ...]]] = [
        (sha, True, window[sha]) for sha in protected_in_window
    ]
    merges += [(sha, False, files) for sha, files in beside_window.items()]
    merges.sort(key=lambda item: item[0])

    config = snapshot.adjacency
    selector = HandlerTestSelectionSelector(
        SourceInMemory(snapshot.durations), snapshot.policy
    )
    fixed = fixed_set_paths(snapshot)
    bound = snapshot.policy.target_shard_seconds * snapshot.policy.max_narrowed_shards
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="protected-surface-") as scratch:
        root = Path(scratch)
        materialize_tree(snapshot, root)
        try:
            fixed_seconds = _seconds(selector, root, fixed)
            for sha, in_window, changed in merges:
                resolved = HandlerTestPathResolution().handle(
                    ModelPathResolutionRequest(
                        changed_files=changed, adjacency=config, repo_root=str(root)
                    )
                )
                closure = list(resolved.selected_paths)
                if WHOLE_SUITE not in closure:
                    closure = sorted(
                        set(closure) | config.narrowed_extra_paths(changed)
                    )
                closure_paths = tuple(sorted(closure))
                union = tuple(sorted(set(closure_paths) | set(fixed)))
                closure_seconds = _seconds(selector, root, closure_paths)
                union_seconds = _seconds(selector, root, union)
                rows.append(
                    {
                        "sha": sha,
                        "in_replay_window": in_window,
                        "changed_files": list(changed),
                        "closure_paths": list(closure_paths),
                        "closure_seconds": closure_seconds,
                        "closure_plus_fixed_paths": list(union),
                        "closure_plus_fixed_seconds": union_seconds,
                        "closure_under_bound": closure_seconds <= bound,
                        "closure_plus_fixed_under_bound": union_seconds <= bound,
                    }
                )
        finally:
            resolution.clear_import_graph_cache()

    moved = [r["sha"] for r in rows if r["closure_plus_fixed_under_bound"]]
    return {
        "schema_version": 1,
        "replay_input_path": REPLAY_INPUT_PATH,
        "replay_input_sha256": _sha256(REPO_ROOT / REPLAY_INPUT_PATH),
        "tree_id": replay_input["tree_id"],
        "durations_sha256": json.loads(BASELINE_PATH.read_text(encoding="utf-8"))[
            "durations"
        ]["file_sha256"],
        "durations_total_seconds": round(sum(snapshot.durations.values()), 6),
        "bound": {
            "target_shard_seconds": snapshot.policy.target_shard_seconds,
            "max_narrowed_shards": snapshot.policy.max_narrowed_shards,
            "bound_seconds": bound,
        },
        "fixed_set": {"paths": list(fixed), "seconds": fixed_seconds},
        "window_merges": len(replay_input["merges"]),
        "merges": rows,
        "moved_under_bound": moved,
        "end_state": (
            EnumProtectedEndState.A_MERGE_MOVES_UNDER_BOUND
            if moved
            else EnumProtectedEndState.NO_MERGE_MOVES_UNDER_BOUND
        ).value,
    }


# --- fixtures ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def receipt_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def receipt(receipt_document: dict[str, Any]) -> ModelProtectedSurfaceReceipt:
    return ModelProtectedSurfaceReceipt.model_validate(receipt_document)


@pytest.fixture(scope="module")
def replay_input() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / REPLAY_INPUT_PATH).read_text(encoding="utf-8")
    )
    return document


@pytest.fixture(scope="module")
def snapshot(replay_input: dict[str, Any]) -> ModelTreeSnapshot:
    return read_snapshot_file(
        str(REPO_ROOT), replay_input["snapshot_path"], replay_input["snapshot_sha256"]
    )


@pytest.fixture(scope="module")
def baseline_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return document


def protected_in_window(baseline_document: dict[str, Any]) -> tuple[str, ...]:
    """The merges the baseline replay sent to the full suite for the protected-surface reason."""
    return tuple(
        sorted(
            d["sha"]
            for d in baseline_document["counterfactual"]["decisions"]
            if d["primary_reason"] == PROTECTED_REASON
        )
    )


def beside_window(
    receipt: ModelProtectedSurfaceReceipt,
) -> dict[str, tuple[str, ...]]:
    return {m.sha: m.changed_files for m in receipt.merges if not m.in_replay_window}


def recompute(
    receipt: ModelProtectedSurfaceReceipt,
    snapshot: ModelTreeSnapshot,
    replay_input: dict[str, Any],
    baseline_document: dict[str, Any],
) -> dict[str, Any]:
    return measure_protected_surface(
        snapshot,
        replay_input,
        protected_in_window(baseline_document),
        beside_window(receipt),
    )


def missing_merges(
    receipt: ModelProtectedSurfaceReceipt, baseline_document: dict[str, Any]
) -> list[str]:
    """Protected merges, by sha, that the receipt does not state (empty when it states every one)."""
    wanted = {*NAMED_MERGES, *protected_in_window(baseline_document)}
    return sorted(wanted - {m.sha for m in receipt.merges})


# --- the receipt ------------------------------------------------------------------------------------------


def test_protected_surface_set_receipt_validates_and_is_canonical(
    receipt: ModelProtectedSurfaceReceipt, receipt_document: dict[str, Any]
) -> None:
    assert RECEIPT_PATH.read_text(encoding="utf-8") == canonical(receipt_document)
    assert receipt.window_merges == 60


def test_protected_surface_set_states_every_protected_merge(
    receipt: ModelProtectedSurfaceReceipt, baseline_document: dict[str, Any]
) -> None:
    assert missing_merges(receipt, baseline_document) == []
    window = {m.sha for m in receipt.merges if m.in_replay_window}
    assert window == set(protected_in_window(baseline_document))


def test_protected_surface_set_missing_merge_fails(
    receipt: ModelProtectedSurfaceReceipt, baseline_document: dict[str, Any]
) -> None:
    dropped = receipt.merges[0].sha
    shorter = receipt.model_copy(update={"merges": receipt.merges[1:]})
    assert missing_merges(shorter, baseline_document) == [dropped]


def test_protected_surface_set_recomputes_every_figure_from_the_frozen_tree(
    receipt: ModelProtectedSurfaceReceipt,
    receipt_document: dict[str, Any],
    snapshot: ModelTreeSnapshot,
    replay_input: dict[str, Any],
    baseline_document: dict[str, Any],
) -> None:
    derived = recompute(receipt, snapshot, replay_input, baseline_document)
    assert derived == receipt_document


def test_protected_surface_set_wrong_value_fails(
    receipt: ModelProtectedSurfaceReceipt,
    receipt_document: dict[str, Any],
    snapshot: ModelTreeSnapshot,
    replay_input: dict[str, Any],
    baseline_document: dict[str, Any],
) -> None:
    """One altered duration in a copy of the frozen tree moves a recomputed figure off the receipt."""
    delegation = next(
        path
        for path in snapshot.durations
        if path.startswith("tests/unit/delegation/") and snapshot.durations[path] > 0
    )
    changed = dict(snapshot.durations)
    changed[delegation] += 1.0
    altered = snapshot.model_copy(update={"durations": changed})
    derived = recompute(receipt, altered, replay_input, baseline_document)
    assert derived != receipt_document
    assert derived["fixed_set"]["seconds"] == pytest.approx(
        receipt.fixed_set.seconds + 1.0
    )


def test_protected_surface_set_durations_digest_is_the_baseline_digest(
    receipt: ModelProtectedSurfaceReceipt,
    snapshot: ModelTreeSnapshot,
    baseline_document: dict[str, Any],
) -> None:
    assert receipt.durations_sha256 == baseline_document["durations"]["file_sha256"]
    assert receipt.durations_total_seconds == round(sum(snapshot.durations.values()), 6)
    assert (
        receipt.durations_total_seconds
        == baseline_document["durations"]["total_seconds"]
    )
    assert receipt.replay_input_sha256 == _sha256(REPO_ROOT / REPLAY_INPUT_PATH)
    assert receipt.tree_id == baseline_document["tree_id"]


def test_protected_surface_set_in_window_changed_files_are_the_replay_inputs(
    receipt: ModelProtectedSurfaceReceipt, replay_input: dict[str, Any]
) -> None:
    window = {m["sha"]: tuple(m["changed_files"]) for m in replay_input["merges"]}
    for merge in receipt.merges:
        if merge.in_replay_window:
            assert merge.changed_files == window[merge.sha]
        else:
            assert merge.sha not in window


# --- the bound and the end state --------------------------------------------------------------------------


def test_protected_surface_set_bound_is_the_pinned_policy(
    receipt: ModelProtectedSurfaceReceipt, snapshot: ModelTreeSnapshot
) -> None:
    assert receipt.bound.target_shard_seconds == snapshot.policy.target_shard_seconds
    assert receipt.bound.max_narrowed_shards == snapshot.policy.max_narrowed_shards
    assert receipt.bound.bound_seconds == (
        snapshot.policy.target_shard_seconds * snapshot.policy.max_narrowed_shards
    )


def test_protected_surface_set_fixed_set_is_the_delegation_tests_of_the_frozen_tree(
    receipt: ModelProtectedSurfaceReceipt, snapshot: ModelTreeSnapshot
) -> None:
    assert receipt.fixed_set.paths == fixed_set_paths(snapshot)
    assert receipt.fixed_set.paths[:2] == FIXED_DIRECTORIES
    flat = receipt.fixed_set.paths[2:]
    assert flat
    assert all(p.count("/") == 1 and "delegat" in p for p in flat)


def test_protected_surface_set_no_merge_moves_under_the_bound(
    receipt: ModelProtectedSurfaceReceipt,
) -> None:
    """The measured outcome: every protected merge stays over the bound, with or without the fixed set."""
    assert receipt.end_state is EnumProtectedEndState.NO_MERGE_MOVES_UNDER_BOUND
    assert receipt.moved_under_bound == ()
    for merge in receipt.merges:
        assert merge.closure_seconds > receipt.bound.bound_seconds
        assert merge.closure_plus_fixed_seconds > receipt.bound.bound_seconds
        assert merge.closure_plus_fixed_seconds >= merge.closure_seconds


def test_protected_surface_set_full_suite_rule_stays_in_the_adjacency_file(
    snapshot: ModelTreeSnapshot,
) -> None:
    """With no merge under the bound the whole-suite rule is unchanged and no narrowed key exists."""
    assert snapshot.adjacency.full_suite_path_globs
    text = (REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml").read_text(
        encoding="utf-8"
    )
    assert "full_suite_path_globs:" in text
    assert "protected_surface_selected_paths" not in text


def test_protected_surface_set_stage_stays_zero() -> None:
    policy = (
        REPO_ROOT
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_test_selection_selector_compute"
        / "policy.yaml"
    ).read_text(encoding="utf-8")
    assert "\nselection_stage: 0\n" in policy


# --- the receipt model's own refusals ---------------------------------------------------------------------


def test_protected_surface_set_model_refuses_an_end_state_its_figures_contradict(
    receipt_document: dict[str, Any],
) -> None:
    contradicting = {
        **receipt_document,
        "end_state": EnumProtectedEndState.A_MERGE_MOVES_UNDER_BOUND.value,
    }
    with pytest.raises(ValidationError):
        ModelProtectedSurfaceReceipt.model_validate(contradicting)


def test_protected_surface_set_model_refuses_a_bound_that_is_not_the_product(
    receipt_document: dict[str, Any],
) -> None:
    bad = {
        **receipt_document,
        "bound": {**receipt_document["bound"], "bound_seconds": 1.0},
    }
    with pytest.raises(ValidationError):
        ModelProtectedSurfaceReceipt.model_validate(bad)


def test_protected_surface_set_model_refuses_a_duplicate_merge(
    receipt_document: dict[str, Any],
) -> None:
    first = receipt_document["merges"][0]
    bad = {**receipt_document, "merges": [first, first]}
    with pytest.raises(ValidationError):
        ModelProtectedSurfaceReceipt.model_validate(bad)
