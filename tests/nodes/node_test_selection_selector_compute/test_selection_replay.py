# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the selection replay and its committed baseline receipt.

The first half pins a six-merge fixture over a frozen synthetic tree: the replay must give the decisions the
live selector (``compute_selection``) gives, and must differ when the adjacency map is edited. The second
half holds the committed receipt of the last 60 merges to its acceptance lines: its counts re-derive from its
own changed-file lists, its totals are the sums of its rows, its observed section is explicit about what it
could not read, and the counterfactual re-derives byte for byte from the replay input.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_tree_snapshot import (
    build_tree_snapshot,
    read_snapshot_file,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
    changed_sections,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_baseline_receipt import (
    ModelBaselineReceipt,
    ModelObservedSection,
    ModelObservedValue,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayFailed,
    ModelReplayMerge,
    ModelReplayRequest,
    ModelReplayResult,
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
)
from scripts.ci.detect_test_paths import compute_selection

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "config" / "test_selection_replay_receipt.json"
TREE_ID = "0123456789abcdef0123456789abcdef01234567"
CATEGORIES = {"pyproject.toml": "pyproject", "uv.lock": "lock", "scripts/": "scripts"}

#: What the replay measured over the last 60 merges at the pinned head (it differs from the plan's figures).
MEASURED_MERGES = 60
MEASURED_NARROWED = 10
MEASURED_FULL = 50
MEASURED_REASONS = {
    "narrowed_selection_too_heavy": 8,
    "no_narrowable_selection": 5,
    "protected_surface": 3,
    "shared_module": 8,
    "test_infrastructure": 22,
    "unmapped_module": 4,
}


def canonical(document: Any) -> str:
    return json.dumps(document, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


# --- the pinned six-merge fixture -------------------------------------------------------------------------


def _adjacency() -> ModelAdjacencyMap:
    return ModelAdjacencyMap.model_validate(
        {
            "schema_version": 1,
            "shared_modules": ["models"],
            "thresholds": {"modules_changed_for_full_suite": 6},
            "test_infrastructure_paths": ["pyproject.toml", "uv.lock", "scripts/"],
            "full_suite_path_globs": ["src/omnimarket/inference/**"],
            "adjacency": {
                "models": {"reverse_deps": ["nodes"]},
                "nodes": {"reverse_deps": []},
                "classifiers": {"reverse_deps": []},
                "inference": {"reverse_deps": ["nodes"]},
            },
            "always_selected_paths": ["tests/gates/"],
            "node_scoped_modules": ["nodes"],
        }
    )


def _write(root: Path, rel: str, text: str = "") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A small checkout, and the adjacency file ``compute_selection`` reads."""
    root = tmp_path / "tree"
    pkg = "src/omnimarket"
    _write(root, f"{pkg}/__init__.py")
    _write(root, f"{pkg}/nodes/__init__.py")
    _write(root, f"{pkg}/nodes/node_alpha/__init__.py")
    _write(root, f"{pkg}/nodes/node_alpha/handlers/__init__.py")
    _write(root, f"{pkg}/nodes/node_alpha/handlers/handler.py", "X = 1\n")
    _write(root, f"{pkg}/models/__init__.py")
    _write(root, f"{pkg}/models/shared.py", "Y = 1\n")
    _write(root, f"{pkg}/classifiers/__init__.py")
    _write(root, f"{pkg}/classifiers/classify.py", "Z = 1\n")
    _write(root, f"{pkg}/inference/__init__.py")
    _write(root, f"{pkg}/inference/infer.py", "W = 1\n")
    _write(root, "tests/__init__.py")
    _write(root, "tests/nodes/__init__.py")
    _write(root, "tests/nodes/node_alpha/__init__.py")
    _write(
        root,
        "tests/nodes/node_alpha/test_alpha.py",
        "from omnimarket.nodes.node_alpha.handlers.handler import X\n",
    )
    _write(
        root,
        "tests/classifiers/test_classify.py",
        "from omnimarket.classifiers import classify\n",
    )
    _write(root, "tests/gates/test_gate.py")
    _write(root, "tests/test_root_level.py")
    _write(
        root,
        "config/test_file_durations.json",
        json.dumps(
            {
                "tests/nodes/node_alpha/test_alpha.py": 40.0,
                "tests/classifiers/test_classify.py": 20.0,
                "tests/gates/test_gate.py": 5.0,
                "tests/test_root_level.py": 1.0,
            }
        ),
    )
    adjacency_file = tmp_path / "adjacency.yaml"
    adjacency_file.write_text(
        yaml.safe_dump(_adjacency().model_dump(mode="json")), encoding="utf-8"
    )
    return root, adjacency_file


FIXTURE_MERGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("a" * 40, ("docs/readme.md",)),
    ("b" * 40, ("src/omnimarket/classifiers/classify.py",)),
    ("c" * 40, ("src/omnimarket/models/shared.py",)),
    ("d" * 40, ("pyproject.toml",)),
    ("e" * 40, ("src/omnimarket/inference/infer.py",)),
    ("f" * 40, ("tests/test_root_level.py",)),
)
PYPROJECT_BASE = '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 88\n'
PYPROJECT_HEAD = '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 100\n'


def _snapshot(root: Path) -> ModelTreeSnapshot:
    return build_tree_snapshot(root, TREE_ID, _adjacency(), load_policy())


def _request(
    snapshot: ModelTreeSnapshot,
    merges: tuple[tuple[str, tuple[str, ...]], ...] = FIXTURE_MERGES,
) -> ModelReplayRequest:
    return ModelReplayRequest.model_validate(
        {
            "tree_id": TREE_ID,
            "snapshot": snapshot.model_dump(mode="json"),
            "full_split_count": 20,
            "infrastructure_categories": CATEGORIES,
            "merges": [
                {
                    "sha": sha,
                    "changed_files": list(files),
                    "data_files": (
                        [
                            {
                                "path": "pyproject.toml",
                                "base_text": PYPROJECT_BASE,
                                "head_text": PYPROJECT_HEAD,
                            }
                        ]
                        if "pyproject.toml" in files
                        else []
                    ),
                }
                for sha, files in merges
            ],
        }
    )


def _replay(request: ModelReplayRequest) -> ModelReplayResult:
    result = HandlerSelectionReplay().handle(request)
    assert isinstance(result, ModelReplayResult), result
    return result


def test_replay_fixture_gives_the_decisions_the_live_selector_gives(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, adjacency_file = fixture_tree
    result = _replay(_request(_snapshot(root)))

    assert len(result.decisions) == 6
    reasons = [d.primary_reason for d in result.decisions]
    assert reasons == [
        "no_narrowable_selection",
        None,
        "shared_module",
        "test_infrastructure",
        "protected_surface",
        None,
    ]
    for decision, (_, files) in zip(result.decisions, FIXTURE_MERGES, strict=True):
        live = compute_selection(
            list(files), adjacency_file, "dev", "pull_request", True, repo_root=root
        )
        assert decision.kind.value == (
            "full_suite" if live.is_full_suite else "narrowed"
        )
        assert decision.primary_reason == (
            live.full_suite_reason.value if live.full_suite_reason else None
        )
        assert list(decision.selected_paths) == live.selected_paths
        assert decision.split_count == live.split_count


def test_replay_differs_when_full_suite_path_globs_is_edited(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, _ = fixture_tree
    snapshot = _snapshot(root)
    before = _replay(_request(snapshot))
    edited = snapshot.model_copy(
        update={
            "adjacency": snapshot.adjacency.model_copy(
                update={
                    "full_suite_path_globs": [
                        *snapshot.adjacency.full_suite_path_globs,
                        "src/omnimarket/classifiers/**",
                    ]
                }
            )
        }
    )
    after = _replay(_request(edited))

    assert before.decisions[1].primary_reason is None
    assert after.decisions[1].primary_reason == "protected_surface"
    assert before.model_dump_json() != after.model_dump_json()
    assert after.totals.narrowed == before.totals.narrowed - 1


def test_replay_reports_every_trigger_a_merge_hit(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, _ = fixture_tree
    request = _request(
        _snapshot(root),
        (
            (
                "1" * 40,
                (
                    "scripts/tool.py",
                    "uv.lock",
                    "src/omnimarket/inference/infer.py",
                    "src/omnimarket/models/shared.py",
                ),
            ),
        ),
    )
    decision = _replay(request).decisions[0]

    assert decision.primary_reason == "test_infrastructure"
    assert [(t.reason, t.evidence, t.entry) for t in decision.triggers] == [
        ("test_infrastructure", "scripts/tool.py", "scripts/"),
        ("test_infrastructure", "uv.lock", "uv.lock"),
        (
            "protected_surface",
            "src/omnimarket/inference/infer.py",
            "src/omnimarket/inference/**",
        ),
        ("shared_module", "models", None),
    ]


def test_replay_cross_tab_names_each_infrastructure_merge_once(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, _ = fixture_tree
    request = _request(
        _snapshot(root),
        (
            *FIXTURE_MERGES,
            ("2" * 40, ("scripts/x.py", "uv.lock")),
        ),
    )
    tab = _replay(request).infrastructure_cross_tab

    assert tab.merges == 2
    assert tab.cells == {"lock+scripts": 1, "pyproject": 1}
    assert sum(tab.cells.values()) == tab.merges
    assert tab.rows[0].changed_sections == {"pyproject.toml": ("tool.ruff",)}


def test_replay_changed_sections_are_the_tables_whose_lines_differ() -> None:
    assert changed_sections(PYPROJECT_BASE, PYPROJECT_HEAD) == ("tool.ruff",)
    assert changed_sections(PYPROJECT_BASE, PYPROJECT_BASE) == ()
    assert changed_sections("", "[a]\nx = 1\n") == ("a",)
    assert changed_sections("top = 1\n", "top = 2\n") == ("",)


def test_replay_unreadable_snapshot_is_a_typed_failure(
    fixture_tree: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = fixture_tree
    request = _request(_snapshot(root))
    by_path = request.model_copy(
        update={
            "snapshot": None,
            "snapshot_path": "missing.json.gz",
            "snapshot_sha256": "0" * 64,
            "repo_root": str(tmp_path),
        }
    )
    failed = HandlerSelectionReplay().handle(by_path)
    assert isinstance(failed, ModelReplayFailed)
    assert failed.terminal_failure_cause == failed.reason


def test_replay_snapshot_of_other_bytes_is_refused(tmp_path: Path) -> None:
    (tmp_path / "snap.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="sha256"):
        read_snapshot_file(str(tmp_path), "snap.json", "0" * 64)


def test_replay_is_stage_zero_only() -> None:
    with pytest.raises(ValidationError):
        ModelReplayRequest.model_validate({"stage": 1})


def test_replay_request_needs_exactly_one_snapshot_source(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, _ = fixture_tree
    request = _request(_snapshot(root))
    with pytest.raises(ValidationError):
        request.model_copy(update={"snapshot": None}).model_validate(
            request.model_copy(update={"snapshot": None}).model_dump(mode="json")
        )


def test_replay_infrastructure_entry_without_a_category_is_a_typed_failure(
    fixture_tree: tuple[Path, Path],
) -> None:
    root, _ = fixture_tree
    request = _request(_snapshot(root)).model_copy(
        update={"infrastructure_categories": {"pyproject.toml": "pyproject"}}
    )
    assert isinstance(HandlerSelectionReplay().handle(request), ModelReplayFailed)


# --- the committed baseline receipt -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def receipt_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def receipt(receipt_document: dict[str, Any]) -> ModelBaselineReceipt:
    return ModelBaselineReceipt.model_validate(receipt_document)


@pytest.fixture(scope="module")
def replay_input(receipt: ModelBaselineReceipt) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / receipt.replay_input_path).read_text(encoding="utf-8")
    )
    return document


@pytest.fixture(scope="module")
def stage_zero_replay(replay_input: dict[str, Any]) -> ModelReplayResult:
    return _replay(ModelReplayRequest.model_validate(replay_input))


@pytest.fixture(scope="module")
def frozen_durations(receipt: ModelBaselineReceipt) -> ModelTreeSnapshot:
    return read_snapshot_file(
        str(REPO_ROOT),
        "config/test_selection_replay_tree.json.gz",
        json.loads((REPO_ROOT / receipt.replay_input_path).read_text())[
            "snapshot_sha256"
        ],
    )


def _durations_agree(
    receipt: ModelBaselineReceipt,
    snapshot: ModelTreeSnapshot,
    durations: dict[str, float],
) -> list[str]:
    """What is wrong with the receipt's durations reading, given the recorded durations (empty when sound)."""
    problems: list[str] = []
    if round(sum(durations.values()), 6) != receipt.durations.total_seconds:
        problems.append("total_seconds is not the sum of the durations file")
    if len(durations) != receipt.durations.entries:
        problems.append("entries is not the durations file's entry count")
    test_files = {
        path
        for path in snapshot.files
        if path.startswith("tests/")
        and (path.rsplit("/", 1)[-1].startswith("test_") or path.endswith("_test.py"))
    }
    extras = sorted(k for k in durations if k not in test_files)
    if tuple(extras) != receipt.durations.extra_entries:
        problems.append("extra_entries are not the entries naming no test file")
    if len(test_files) != receipt.durations.test_files:
        problems.append("test_files is not the count of test files")
    if (
        round(sum(durations[k] for k in extras), 6)
        != receipt.durations.extra_entries_seconds
    ):
        problems.append("extra_entries_seconds is not their sum")
    return problems


def test_baseline_receipt_counts_rederive_from_its_own_changed_file_lists(
    receipt: ModelBaselineReceipt,
) -> None:
    rows = receipt.counterfactual.decisions
    request = ModelReplayRequest.model_validate(
        json.loads((REPO_ROOT / receipt.replay_input_path).read_text())
    ).model_copy(
        update={
            "merges": tuple(
                ModelReplayMerge(sha=r.sha, changed_files=r.changed_files) for r in rows
            )
        }
    )
    derived = _replay(request).totals

    assert derived.merges == len(rows) == MEASURED_MERGES
    assert (
        derived.narrowed == receipt.counterfactual.totals.narrowed == MEASURED_NARROWED
    )
    assert (
        derived.full_suite == receipt.counterfactual.totals.full_suite == MEASURED_FULL
    )
    assert derived.reason_counts == receipt.counterfactual.totals.reason_counts
    assert derived.reason_counts == MEASURED_REASONS


def test_baseline_receipt_states_the_plan_figures_it_replaces(
    receipt: ModelBaselineReceipt,
) -> None:
    checks = {c.premise: c for c in receipt.premise_checks}
    assert checks["merges narrowed"].plan_value == "8"
    assert checks["merges narrowed"].measured_value == str(MEASURED_NARROWED)
    assert not checks["merges narrowed"].holds
    assert checks["merges full suite"].measured_value == str(MEASURED_FULL)
    for reason, count in MEASURED_REASONS.items():
        assert checks[f"full-suite merges, reason {reason}"].measured_value == str(
            count
        )
    # dev's selector selects a changed root-level test file instead of taking the full suite (the reason
    # changed_test_unnarrowable), so no merge of the replay holds it now; the plan figure stays beside it.
    unnarrowable = checks["full-suite merges, reason changed_test_unnarrowable"]
    assert unnarrowable.plan_value == "4"
    assert unnarrowable.measured_value == "0"
    assert not unnarrowable.holds


def test_baseline_receipt_total_seconds_equals_the_durations_sum(
    receipt: ModelBaselineReceipt, frozen_durations: ModelTreeSnapshot
) -> None:
    assert _durations_agree(receipt, frozen_durations, frozen_durations.durations) == []
    assert round(receipt.durations.total_seconds) == 11419
    assert len(receipt.durations.extra_entries) == receipt.durations.entries - len(
        [
            k
            for k in frozen_durations.durations
            if k not in receipt.durations.extra_entries
        ]
    )


def test_baseline_receipt_fails_when_one_duration_changes_in_a_fixture_copy(
    receipt: ModelBaselineReceipt, frozen_durations: ModelTreeSnapshot
) -> None:
    changed = dict(frozen_durations.durations)
    first = next(k for k in changed if k not in receipt.durations.extra_entries)
    changed[first] += 1.0

    assert _durations_agree(receipt, frozen_durations, changed) == [
        "total_seconds is not the sum of the durations file"
    ]


def test_baseline_receipt_lists_the_entries_beyond_the_test_files(
    receipt: ModelBaselineReceipt,
) -> None:
    assert receipt.durations.extra_entries
    assert receipt.durations.extra_entries_counted_in_total is True
    assert receipt.durations.extra_entries_seconds > 0


def test_baseline_receipt_cross_tab_settles_which_merges_changed_pyproject(
    receipt: ModelBaselineReceipt,
) -> None:
    tab = receipt.counterfactual.infrastructure_cross_tab
    assert tab.merges == MEASURED_REASONS["test_infrastructure"] == 22
    assert sum(tab.cells.values()) == 22
    with_pyproject = [r for r in tab.rows if "pyproject" in r.categories]
    assert len(with_pyproject) == 18
    assert {r.sha for r in tab.rows if "pyproject" not in r.categories} == {
        r.sha for r in tab.rows if r.categories == ("fixtures",)
    }


# --- observed section -------------------------------------------------------------------------------------


def _observed(receipt_document: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = json.loads(json.dumps(receipt_document["observed"]))
    return section


def test_observed_section_of_the_receipt_is_valid_and_has_the_three_keys(
    receipt: ModelBaselineReceipt,
) -> None:
    section = receipt.observed
    for value in (
        section.enable_smart_tests,
        section.runner_minutes_median,
        section.runner_minutes_p90,
        section.shard_setup_seconds,
    ):
        assert value.state in ("read", "unavailable")
        if value.state == "unavailable":
            assert value.value is None
            assert value.reason


@pytest.mark.parametrize(
    "key",
    [
        "enable_smart_tests",
        "runner_minutes_median",
        "runner_minutes_p90",
        "shard_setup_seconds",
    ],
)
def test_observed_section_without_a_key_fails_validation(
    receipt_document: dict[str, Any], key: str
) -> None:
    section = _observed(receipt_document)
    del section[key]
    with pytest.raises(ValidationError):
        ModelObservedSection.model_validate(section)


def test_observed_section_refuses_a_bare_number() -> None:
    with pytest.raises(ValidationError):
        ModelObservedValue.model_validate(
            {"state": "read", "value": 12.5, "sample_size": 3}
        )


def test_observed_section_refuses_a_read_value_with_no_run_id() -> None:
    with pytest.raises(ValidationError):
        ModelObservedValue.model_validate(
            {"state": "read", "value": 12.5, "sample_size": 3, "source_run_ids": []}
        )


def test_observed_section_refuses_a_read_statistic_with_no_sample_size() -> None:
    with pytest.raises(ValidationError):
        ModelObservedValue.model_validate(
            {"state": "read", "value": 12.5, "sample_size": 0, "source_run_ids": [1]}
        )


def test_observed_section_refuses_an_unavailable_value_that_is_a_zero() -> None:
    with pytest.raises(ValidationError):
        ModelObservedValue.model_validate(
            {"state": "unavailable", "value": 0, "sample_size": 0, "reason": "x"}
        )
    with pytest.raises(ValidationError):
        ModelObservedValue.model_validate(
            {"state": "unavailable", "value": None, "sample_size": 0}
        )


def test_observed_section_accepts_a_read_statistic_with_runs_and_a_sample() -> None:
    value = ModelObservedValue.model_validate(
        {
            "state": "read",
            "value": 12.5,
            "unit": "minutes",
            "sample_size": 3,
            "source_run_ids": [1, 2, 3],
        }
    )
    assert value.sample_size == 3


def test_observed_section_is_the_frozen_input_file_and_never_rederived(
    receipt: ModelBaselineReceipt, receipt_document: dict[str, Any]
) -> None:
    path = REPO_ROOT / receipt.observed_input_path
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest() == receipt.observed_input_sha256
    )
    assert json.loads(path.read_text()) == receipt_document["observed"]


# --- totals, rows, cross-tab ------------------------------------------------------------------------------


def test_receipt_totals_have_sixty_rows_each_with_a_reason_and_a_stage_zero_outcome(
    receipt: ModelBaselineReceipt,
) -> None:
    rows = receipt.counterfactual.decisions
    assert len(rows) == 60
    for row in rows:
        assert row.kind.value in ("narrowed", "full_suite")
        assert row.reason_code
        assert (row.primary_reason is None) == (row.kind.value == "narrowed")
        assert bool(row.triggers) == (row.kind.value == "full_suite")
    assert receipt.counterfactual.stage == 0
    totals = receipt.counterfactual.totals
    assert totals.selected_seconds == round(sum(r.selected_seconds for r in rows), 6)
    assert totals.selected_shards == sum(r.split_count for r in rows)
    assert Counter(r.primary_reason for r in rows if r.primary_reason) == Counter(
        totals.reason_counts
    )


def test_receipt_totals_fail_when_one_row_outcome_is_edited(
    receipt_document: dict[str, Any],
) -> None:
    edited = json.loads(json.dumps(receipt_document))
    row = next(
        r for r in edited["counterfactual"]["decisions"] if r["kind"] == "narrowed"
    )
    row["kind"] = "full_suite"
    with pytest.raises(ValidationError):
        ModelBaselineReceipt.model_validate(edited)


def test_receipt_totals_fail_when_a_row_seconds_value_is_edited(
    receipt_document: dict[str, Any],
) -> None:
    edited = json.loads(json.dumps(receipt_document))
    edited["counterfactual"]["decisions"][0]["selected_seconds"] += 1.0
    with pytest.raises(ValidationError):
        ModelBaselineReceipt.model_validate(edited)


def test_receipt_totals_fail_when_a_row_is_dropped(
    receipt_document: dict[str, Any],
) -> None:
    edited = json.loads(json.dumps(receipt_document))
    del edited["counterfactual"]["decisions"][-1]
    with pytest.raises(ValidationError):
        ModelBaselineReceipt.model_validate(edited)


def test_receipt_totals_fail_when_cross_tab_cells_do_not_sum_to_the_infrastructure_merges(
    receipt_document: dict[str, Any],
) -> None:
    edited = json.loads(json.dumps(receipt_document))
    cells = edited["counterfactual"]["infrastructure_cross_tab"]["cells"]
    cells[next(iter(cells))] += 1
    with pytest.raises(ValidationError, match="cross-tab"):
        ModelBaselineReceipt.model_validate(edited)


# --- reproducibility and the stage 0 baseline -------------------------------------------------------------


def test_replay_reproducible_input_and_tree_rederive_the_counterfactual_byte_for_byte(
    receipt: ModelBaselineReceipt,
    receipt_document: dict[str, Any],
    stage_zero_replay: ModelReplayResult,
) -> None:
    input_path = REPO_ROOT / receipt.replay_input_path
    assert (
        hashlib.sha256(input_path.read_bytes()).hexdigest()
        == receipt.replay_input_sha256
    )
    request = json.loads(input_path.read_text())
    assert request["tree_id"] == receipt.tree_id
    assert canonical(stage_zero_replay.model_dump(mode="json")) == canonical(
        receipt_document["counterfactual"]
    )


def test_replay_reproducible_fails_when_one_changed_file_list_is_altered(
    receipt_document: dict[str, Any], replay_input: dict[str, Any]
) -> None:
    altered = json.loads(json.dumps(replay_input))
    # The first narrowed merge now also changes a file under the test infrastructure.
    narrowed = {
        r["sha"]
        for r in receipt_document["counterfactual"]["decisions"]
        if r["kind"] == "narrowed"
    }
    merge = next(m for m in altered["merges"] if m["sha"] in narrowed)
    merge["changed_files"] = [*merge["changed_files"], "uv.lock"]
    rederived = _replay(ModelReplayRequest.model_validate(altered))

    assert canonical(rederived.model_dump(mode="json")) != canonical(
        receipt_document["counterfactual"]
    )


def test_replay_reproducible_input_names_the_snapshot_it_was_made_against(
    receipt: ModelBaselineReceipt, replay_input: dict[str, Any]
) -> None:
    snapshot = read_snapshot_file(
        str(REPO_ROOT), replay_input["snapshot_path"], replay_input["snapshot_sha256"]
    )
    assert snapshot.tree_id == receipt.tree_id == replay_input["tree_id"]
    assert receipt.counterfactual.snapshot_sha256 == replay_input["snapshot_sha256"]


def test_baseline_equals_a_stage_zero_replay_and_a_later_edit_does_not_slip_by(
    receipt: ModelBaselineReceipt,
    receipt_document: dict[str, Any],
    stage_zero_replay: ModelReplayResult,
) -> None:
    assert (
        stage_zero_replay.model_dump(mode="json") == receipt_document["counterfactual"]
    )
    tampered = json.loads(json.dumps(receipt_document["counterfactual"]))
    tampered["decisions"][0]["split_count"] += 1
    assert stage_zero_replay.model_dump(mode="json") != tampered
    assert receipt.counterfactual.stage == 0
