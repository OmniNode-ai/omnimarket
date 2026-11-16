# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174 AC3: a size cell reads as the capability map reads it, and nothing under the minimum reads as zero."""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    EnumEvalBoardVerdict,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMap,
)
from tests.nodes.node_eval_board_compute.support import (
    FIRST_TEST,
    HOSTED_ID,
    LOCAL_ID,
    live_week,
    model,
    rates,
    request,
    run,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_LINE = ModelCapabilityLine(floor=0.9, margin=0.07, confidence=0.95, power=0.8)
_CONFIGURATION = ModelCapabilityConfiguration()
_IDENTITY = ModelCapabilityIdentity(
    model_revision="rev-1",
    backend="local-url",
    inference_configuration=_CONFIGURATION.configuration_id,
    suite_revision="suite-1",
    grader_revision="tests",
    harness_revision="trial",
)
_BANDS = {"S": EnumSizeBand.S, "M": EnumSizeBand.M, "L": EnumSizeBand.L}


def _capability_verdicts(counts: dict[str, tuple[int, int]]) -> dict[str, str]:
    """The capability map's own verdict per size for the same rows (n tasks, passed of them)."""
    rows = tuple(
        ModelCapabilityVerdictRow(
            tenant_id=_TENANT,
            item_key=f"{band}-{i}",
            source=EnumCapabilitySource.LABELLED_ITEM,
            task_class="code",
            size_band=_BANDS[band],
            model_key="m",
            backend_id="local-url",
            configuration=_CONFIGURATION,
            identity=_IDENTITY,
            passed=i < passed,
        )
        for band, (n, passed) in counts.items()
        for i in range(n)
    )
    folded = HandlerDelegationCapabilityMap().handle(
        ModelCapabilityMapFoldRequest(
            map_run_id="run-1",
            rows=rows,
            assignments=(
                ModelCapabilityServingAssignment(
                    model_key="m",
                    backend_id="local-url",
                    configuration_id=_CONFIGURATION.configuration_id,
                    identity=_IDENTITY,
                ),
            ),
            line=_LINE,
        )
    )
    return {cell.size_band.value: cell.verdict.value for cell in folded.cells}


@pytest.mark.parametrize(
    "counts",
    [
        {"S": (100, 99), "M": (100, 90), "L": (30, 30)},
        {"S": (100, 100), "M": (100, 97), "L": (100, 80)},
        {"S": (60, 60), "M": (89, 84), "L": (89, 70)},
        {"S": (100, 92), "M": (100, 100), "L": (12, 12)},
    ],
)
def test_each_size_cell_verdict_equals_the_capability_maps_for_the_same_rows(
    counts: dict[str, tuple[int, int]],
) -> None:
    expected = _capability_verdicts(counts)
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run(
                        "r1",
                        FIRST_TEST,
                        rows=rates(counts["S"], counts["M"], counts["L"]),
                    ),
                ),
            )
        )
    )
    got = {
        band: cell.verdict.value
        for band, cell in zip("SML", record.chart3.rows[0].cells, strict=True)
        if cell.verdict is not None
    }
    assert got == expected
    assert len(got) == 3


def test_the_comparison_sees_all_three_verdicts_across_the_fixtures() -> None:
    seen: set[str] = set()
    for counts in (
        {"S": (100, 99), "M": (100, 90), "L": (30, 30)},
        {"S": (100, 100), "M": (100, 97), "L": (100, 80)},
    ):
        seen.update(_capability_verdicts(counts).values())
    assert seen == {"MET", "MISSED", "REFUSED"}


def test_nine_tasks_read_too_few_on_a_size_cell_and_a_live_row() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (run("r1", FIRST_TEST, rows=rates((9, 5), (9, 9))),),
                (live_week("live-2026-W45", 9, 4),),
            )
        )
    )
    small, medium, _ = record.chart3.rows[0].cells
    assert small.rate is not None
    assert small.rate.text == "passed 5 of 9 tasks, too few tasks to read"
    assert (small.rate.percent, small.rate.likely_low, small.rate.likely_high) == (
        None,
        None,
        None,
    )
    assert medium.reliable is False
    (live,) = record.live_work.rows
    assert live.rate.text == "passed 4 of 9 tasks, too few tasks to read"
    assert live.rate.percent is None


def test_a_nine_task_chart_one_point_is_hollow_with_no_percentage() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST, rows=rates((9, 5), None, None)),))
        )
    )
    point = record.chart1.series[0].points[0]
    assert point.hollow
    assert point.rate.text == "passed 5 of 9 tasks, too few tasks to read"
    assert point.rate.percent is None
    assert "%" not in point.rate.text


def test_a_rate_at_the_minimum_reads_with_its_count_and_range() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST, rows=rates((15, 9), None, None)),))
        )
    )
    point = record.chart1.series[0].points[0]
    assert not point.hollow
    assert point.rate.text.startswith("passed 9 of 15 tasks (60%, likely ")


@pytest.mark.parametrize(("target", "text"), [(89, "7 of 89"), (50, "7 of 50")])
def test_the_short_band_line_comes_from_the_snapshot(target: int, text: str) -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run(
                        "r1",
                        FIRST_TEST,
                        band_usable={"S": 89, "M": 89, "L": 7},
                        band_target={"S": 89, "M": 89, "L": target},
                    ),
                ),
            )
        )
    )
    assert record.chart3.rows[0].short_band_notes == (
        f"not enough large tasks yet ({text})",
    )


@pytest.mark.parametrize("model_id", [LOCAL_ID, HOSTED_ID])
def test_every_overlay_model_reads_the_capability_maps_verdict_per_size(
    model_id: str,
) -> None:
    """Each model the overlay names gets a size row whose verdicts are the capability map's fold of its rows."""
    counts = {"S": (100, 99), "M": (100, 90), "L": (100, 80)}
    record = HandlerEvalBoard().handle(
        request(
            model(
                model_id,
                (
                    run(
                        "r1",
                        FIRST_TEST,
                        rows=rates(counts["S"], counts["M"], counts["L"]),
                    ),
                ),
            )
        )
    )
    (row,) = record.chart3.rows
    assert [c.verdict.value for c in row.cells if c.verdict is not None] == [
        _capability_verdicts(counts)[band] for band in "SML"
    ]


def test_every_overlay_model_verdict_is_one_the_board_can_name() -> None:
    """A verdict the range line gains must be named by the board before it can reach a size cell."""
    assert {v.name for v in EnumRangeVerdict} == {v.name for v in EnumEvalBoardVerdict}
