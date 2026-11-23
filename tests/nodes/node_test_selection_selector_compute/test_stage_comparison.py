# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""compare_stage_selection: the contract route, the golden chain, the error chain and the repeatability of the comparison."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_file import (
    SourceRecordedSetFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_comparison import (
    HandlerStageComparison,
    selection_runs_test,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelRecordedFullRun,
    ModelStageComparisonRequest,
    ModelStageComparisonResult,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_test_selection_selector_compute"
)


@pytest.fixture(scope="module")
def replayed() -> ModelReplayResult:
    recorded = SourceRecordedSetFile(REPO_ROOT).read_recorded_set(
        "config/test_selection_replay_input.json",
        "config/test_selection_observed_input.json",
    )
    assert recorded is not None
    result = HandlerSelectionReplay().handle(recorded.replay_input)
    assert isinstance(result, ModelReplayResult)
    return result


def _runs(replayed: ModelReplayResult) -> tuple[ModelRecordedFullRun, ...]:
    recorded = SourceRecordedSetFile(REPO_ROOT).read_recorded_set(
        "config/test_selection_replay_input.json",
        "config/test_selection_observed_input.json",
    )
    assert recorded is not None
    return recorded.runs


def _compare(
    replayed: ModelReplayResult,
    runs: tuple[ModelRecordedFullRun, ...],
    minimum: int = 10,
) -> ModelStageComparisonResult:
    return HandlerStageComparison().handle(
        ModelStageComparisonRequest(
            replay=replayed, runs=runs, min_changes_compared=minimum
        )
    )


def _narrowed_sha(replayed: ModelReplayResult) -> tuple[str, tuple[str, ...]]:
    for decision in replayed.decisions:
        if decision.kind == "narrowed":
            return decision.sha, decision.selected_paths
    raise AssertionError("no narrowed change")


def _failure(sha: str, *tests: str) -> ModelRecordedFullRun:
    return ModelRecordedFullRun(
        sha=sha, readable=True, conclusion="failure", failing_tests=tests
    )


def test_contract_routes_compare_stage_selection_to_its_handler() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    routes = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    route = routes["compare_stage_selection"]
    assert route["handler"]["name"] == "HandlerStageComparison"
    module = importlib.import_module(route["handler"]["module"])
    assert hasattr(module, route["handler"]["name"])
    assert route["input_model"].endswith(".ModelStageComparisonRequest")
    assert route["output_model"].endswith(".ModelStageComparisonResult")


def test_golden_chain_the_recorded_set_passes_with_its_counts(
    replayed: ModelReplayResult,
) -> None:
    result = _compare(replayed, _runs(replayed))
    assert result.passed
    assert (
        result.changes_compared,
        result.changes_narrowed,
        result.runs_with_result,
        result.runs_unavailable,
        result.failing_tests,
        result.failing_tests_selected,
        len(result.misses),
    ) == (60, 10, 17, 43, 0, 0, 0)
    assert result.merge_narrowed_share == pytest.approx(10 / 60, abs=1e-6)


def test_golden_chain_the_comparison_is_repeatable_byte_for_byte(
    replayed: ModelReplayResult,
) -> None:
    first = _compare(replayed, _runs(replayed)).model_dump_json()
    second = _compare(replayed, tuple(reversed(_runs(replayed)))).model_dump_json()
    assert first == second


def test_golden_chain_a_failing_test_in_the_selection_is_not_a_miss(
    replayed: ModelReplayResult,
) -> None:
    sha, paths = _narrowed_sha(replayed)
    held = paths[0].rstrip("/") + "/test_x.py::test_y"
    result = _compare(replayed, (_failure(sha, held),))
    assert (result.failing_tests, result.failing_tests_selected) == (1, 1)
    assert result.passed


def test_error_chain_a_failing_test_outside_the_selection_is_a_miss(
    replayed: ModelReplayResult,
) -> None:
    """The positive control: the comparison reports a miss when the selection leaves a failing test out."""
    sha, paths = _narrowed_sha(replayed)
    outside = "tests/test_a_file_no_selection_holds.py::test_y"
    assert not selection_runs_test(paths, outside)
    result = _compare(replayed, (_failure(sha, outside),))
    assert not result.passed
    assert result.reasons == ()  # a miss is its own finding, not a structural reason
    assert [(m.sha, m.test) for m in result.misses] == [(sha, outside)]
    assert result.misses[0].selection_kind == "narrowed"
    assert result.failing_tests_selected == 0


def test_error_chain_a_full_suite_selection_runs_every_failing_test(
    replayed: ModelReplayResult,
) -> None:
    full = next(d for d in replayed.decisions if d.kind == "full_suite")
    result = _compare(
        replayed, (_failure(full.sha, "tests/anything/test_z.py::test_y"),)
    )
    assert result.passed
    assert result.failing_tests_selected == 1


def test_error_chain_a_failing_run_that_names_no_test_is_refused(
    replayed: ModelReplayResult,
) -> None:
    sha, _ = _narrowed_sha(replayed)
    result = _compare(replayed, (_failure(sha),))
    assert not result.passed
    assert result.failing_runs_unnamed == 1


def test_error_chain_no_readable_run_is_not_a_pass(replayed: ModelReplayResult) -> None:
    runs = tuple(
        ModelRecordedFullRun(sha=d.sha, readable=False) for d in replayed.decisions
    )
    result = _compare(replayed, runs)
    assert not result.passed
    assert result.runs_with_result == 0
    assert result.runs_unavailable == 60


def test_error_chain_a_run_for_a_change_the_replay_did_not_decide_is_refused(
    replayed: ModelReplayResult,
) -> None:
    stray = ModelRecordedFullRun(sha="f" * 40, readable=True, conclusion="success")
    result = _compare(replayed, (stray,))
    assert not result.passed
    assert result.runs_unmatched == 1


def test_error_chain_fewer_changes_than_the_policy_asks_for_is_not_a_pass(
    replayed: ModelReplayResult,
) -> None:
    sha, _ = _narrowed_sha(replayed)
    ok = ModelRecordedFullRun(sha=sha, readable=True, conclusion="success")
    assert _compare(replayed, (ok,), minimum=60).passed
    assert not _compare(replayed, (ok,), minimum=61).passed


@pytest.mark.parametrize(
    ("selected", "test", "expected"),
    [
        (("tests/",), "tests/a/test_b.py::t", True),
        (("tests/a/",), "tests/a/test_b.py::t", True),
        (("tests/a",), "tests/a/test_b.py", True),
        (("tests/a/test_b.py",), "tests/a/test_b.py::t", True),
        (("tests/a/",), "tests/ab/test_b.py::t", False),
        (("tests/a/test_b.py",), "tests/a/test_bc.py::t", False),
        ((), "tests/a/test_b.py", False),
    ],
)
def test_selection_runs_test_matches_a_file_or_a_directory_holding_it(
    selected: tuple[str, ...], test: str, expected: bool
) -> None:
    assert selection_runs_test(selected, test) is expected


def test_models_refuse_a_run_whose_readability_and_result_disagree() -> None:
    with pytest.raises(ValidationError):
        ModelRecordedFullRun(sha="a" * 40, readable=False, conclusion="success")
    with pytest.raises(ValidationError):
        ModelRecordedFullRun(sha="a" * 40, readable=True)
    with pytest.raises(ValidationError):
        ModelStageComparisonRequest.model_validate(
            {"replay": {}, "runs": [], "min_changes_compared": 0}
        )
