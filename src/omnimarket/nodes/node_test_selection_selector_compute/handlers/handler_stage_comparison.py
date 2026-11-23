# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compares a stage's narrowed selection with the failures of the recorded full runs (operation ``compare_stage_selection``).

Pure over its request: no clock, no network, no randomness, so the same replay result and the same recorded
runs give the same answer, byte for byte. A change is a merge the replay decided for; its recorded full run
names the tests that failed, and the change's selection must run every one of them. A selection that is the
full suite runs every test.
"""

from __future__ import annotations

from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    EnumReplayKind,
    ModelReplayDecision,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelComparisonMiss,
    ModelRecordedFullRun,
    ModelStageComparisonRequest,
    ModelStageComparisonResult,
)

_SUCCESS = "success"
_WHOLE_TREE = "tests/"


def _test_file(test_id: str) -> str:
    return test_id.split("::", 1)[0]


def selection_runs_test(selected_paths: tuple[str, ...], test_id: str) -> bool:
    """A selection runs a test when one of its paths is the tree, the test's file or a directory holding it."""
    path = _test_file(test_id)
    for selected in selected_paths:
        if selected == _WHOLE_TREE:
            return True
        prefix = selected.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class HandlerStageComparison:
    """Pure over its request."""

    def handle(
        self, request: ModelStageComparisonRequest
    ) -> ModelStageComparisonResult:
        decisions = {d.sha: d for d in request.replay.decisions}
        compared = len(decisions)
        narrowed = sum(d.kind is EnumReplayKind.NARROWED for d in decisions.values())
        runs: dict[str, ModelRecordedFullRun] = {}
        unmatched = 0
        for run in request.runs:
            if run.sha in decisions:
                runs[run.sha] = run
            else:
                unmatched += 1
        readable = [run for sha, run in sorted(runs.items()) if run.readable]
        failing_total = 0
        failing_selected = 0
        failing_runs = 0
        unnamed = 0
        misses: list[ModelComparisonMiss] = []
        for run in readable:
            if run.conclusion != _SUCCESS:
                failing_runs += 1
                if not run.failing_tests:
                    unnamed += 1
            decision = decisions[run.sha]
            for test in sorted(set(run.failing_tests)):
                failing_total += 1
                if selection_runs_test(decision.selected_paths, test):
                    failing_selected += 1
                else:
                    misses.append(_miss(decision, test))
        wall = [run.wall_minutes for run in readable if run.wall_minutes is not None]
        reasons = _reasons(
            request.min_changes_compared,
            compared,
            len(readable),
            unmatched,
            unnamed,
        )
        return ModelStageComparisonResult(
            changes_compared=compared,
            changes_narrowed=narrowed,
            runs_with_result=len(readable),
            runs_unavailable=compared - len(readable),
            runs_unmatched=unmatched,
            failing_runs=failing_runs,
            failing_runs_unnamed=unnamed,
            failing_tests=failing_total,
            failing_tests_selected=failing_selected,
            misses=tuple(misses),
            merge_narrowed_share=round(narrowed / compared, 6) if compared else 0.0,
            test_seconds_per_merge=(
                round(sum(d.selected_seconds for d in decisions.values()) / compared, 6)
                if compared
                else 0.0
            ),
            ci_minutes_per_pr=round(sum(wall) / len(wall), 6) if wall else None,
            passed=not reasons and not misses,
            reasons=reasons,
        )


def _miss(decision: ModelReplayDecision, test: str) -> ModelComparisonMiss:
    return ModelComparisonMiss(
        sha=decision.sha,
        test=test,
        selection_kind=decision.kind.value,
        selected_paths=decision.selected_paths,
    )


def _reasons(
    minimum: int,
    compared: int,
    readable: int,
    unmatched: int,
    unnamed: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if compared < minimum:
        reasons.append(
            f"{compared} changes compared; the policy asks for at least {minimum}"
        )
    if readable < 1:
        reasons.append(
            "no recorded full run is readable, so no failure could be compared"
        )
    if unmatched:
        reasons.append(f"{unmatched} recorded runs belong to no replayed change")
    if unnamed:
        reasons.append(f"{unnamed} failing runs name no failing test")
    return tuple(reasons)
