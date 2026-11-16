# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174 AC2: a marker at a change, and a new task sample ends the line."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    EnumEvalBoardMarkerKind,
    ModelEvalBoardChart1,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalSnapshotRun,
)
from tests.nodes.node_eval_board_compute.support import (
    FIRST_TEST,
    HOSTED_ID,
    LOCAL_ID,
    WEEK,
    model,
    request,
    run,
)

pytestmark = pytest.mark.unit


def _board(*runs: ModelEvalSnapshotRun) -> ModelEvalBoardChart1:
    return HandlerEvalBoard().handle(request(model(LOCAL_ID, tuple(runs)))).chart1


def test_two_model_revisions_give_one_line_and_a_marker() -> None:
    chart = _board(
        run("r1", FIRST_TEST),
        run("r2", FIRST_TEST + WEEK, revision="rev-2"),
    )
    (series,) = chart.series
    assert len(series.points) == 2
    (marker,) = series.markers
    assert (marker.kind, marker.label, marker.point_index) == (
        EnumEvalBoardMarkerKind.MODEL_VERSION,
        "new model version",
        1,
    )
    assert marker.at == FIRST_TEST + WEEK


def test_two_task_samples_give_two_lines_and_the_new_task_set_marker() -> None:
    chart = _board(
        run("r1", FIRST_TEST),
        run("r2", FIRST_TEST + WEEK, sample="sample-b"),
    )
    assert [len(s.points) for s in chart.series] == [1, 1]
    assert chart.series[0].markers == ()
    (marker,) = chart.series[1].markers
    assert (marker.kind, marker.label, marker.point_index) == (
        EnumEvalBoardMarkerKind.TASK_SET,
        "new set of test tasks",
        0,
    )


def _markers_of_new_line(*changes: dict[str, str]) -> list[tuple[int, str]]:
    """The markers on the second line when its first run changes a task sample and `changes`."""
    chart = _board(
        run("r1", FIRST_TEST),
        run("r2", FIRST_TEST + WEEK, sample="sample-b", **changes[0]),
    )
    assert len(chart.series) == 2
    assert chart.series[0].markers == ()
    return [(m.point_index, m.label) for m in chart.series[1].markers]


def test_board_chart1_a_new_task_sample_alone_is_only_the_task_set_marker() -> None:
    assert _markers_of_new_line({}) == [(0, "new set of test tasks")]


def test_board_chart1_a_model_version_change_at_a_new_task_sample_is_marked_at_index_0() -> (
    None
):
    assert _markers_of_new_line({"revision": "rev-2"}) == [
        (0, "new set of test tasks"),
        (0, "new model version"),
    ]


def test_board_chart1_a_prompt_or_settings_change_at_a_new_task_sample_is_marked_at_index_0() -> (
    None
):
    assert _markers_of_new_line({"prompt": "prompt-2"}) == [
        (0, "new set of test tasks"),
        (0, "new prompt or settings"),
    ]
    assert _markers_of_new_line({"settings": "settings-2"}) == [
        (0, "new set of test tasks"),
        (0, "new prompt or settings"),
    ]


def test_board_chart1_a_scorer_change_at_a_new_task_sample_is_marked_at_index_0() -> (
    None
):
    assert _markers_of_new_line({"scorer": "scorer-2"}) == [
        (0, "new set of test tasks"),
        (0, "new way of checking"),
    ]


def test_board_chart1_all_changes_together_at_a_new_task_sample_are_each_marked_at_index_0() -> (
    None
):
    assert _markers_of_new_line(
        {
            "revision": "rev-2",
            "prompt": "prompt-2",
            "scorer": "scorer-2",
        }
    ) == [
        (0, "new set of test tasks"),
        (0, "new model version"),
        (0, "new prompt or settings"),
        (0, "new way of checking"),
    ]


def test_prompt_settings_and_scorer_changes_each_get_their_words() -> None:
    chart = _board(
        run("r1", FIRST_TEST),
        run("r2", FIRST_TEST + WEEK, settings="settings-2"),
        run("r3", FIRST_TEST + 2 * WEEK, settings="settings-2", prompt="prompt-2"),
        run(
            "r4",
            FIRST_TEST + 3 * WEEK,
            settings="settings-2",
            prompt="prompt-2",
            scorer="scorer-2",
        ),
    )
    (series,) = chart.series
    assert [(m.point_index, m.label) for m in series.markers] == [
        (1, "new prompt or settings"),
        (2, "new prompt or settings"),
        (3, "new way of checking"),
    ]


def test_no_change_is_no_marker_and_runs_sort_by_test_time() -> None:
    chart = _board(
        run("r2", FIRST_TEST + WEEK),
        run("r1", FIRST_TEST),
    )
    (series,) = chart.series
    assert series.markers == ()
    assert [p.tested_at for p in series.points] == [FIRST_TEST, FIRST_TEST + WEEK]


def test_a_hosted_model_line_says_the_provider_can_change_it_unmarked() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST),)),
            model(HOSTED_ID, (run("r2", FIRST_TEST, engine="glm"),)),
        )
    )
    notes = {s.model_label: s.note for s in record.chart1.series}
    assert notes["Local coding model (Qwen)"] == ""
    assert (
        notes["Hosted coding model"]
        == "The provider can change this model without a marker."
    )
