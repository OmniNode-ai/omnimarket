# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: a drill lane started from a scratch script is named by the reading within one tick (OMN-22963)."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.handlers import (
    HandlerDispatchReading,
)
from omnimarket.nodes.node_dispatch_reading_compute.handlers.handler_dispatch_reading import (
    SUBJECT_LANES_WITHOUT_RECORD,
)
from omnimarket.nodes.node_dispatch_reading_compute.tests.support import (
    SCRATCH_SCRIPT,
    clean_lanes,
    lane,
    request,
)


def test_scratch_script_named_a_drill_lane_with_no_record_is_named_and_fails() -> None:
    drill = lane("drill-1", kind="drill", launched_from=SCRATCH_SCRIPT)
    result = HandlerDispatchReading().handle(request((*clean_lanes(), drill)))

    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert [(n.lane, n.from_scratch_script) for n in result.lanes_without_record] == [
        ("drill-1", True)
    ]
    subject = {s.name: s for s in result.subjects}[SUBJECT_LANES_WITHOUT_RECORD]
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1
    assert "drill-1" in subject.reason
    assert SCRATCH_SCRIPT in subject.reason


def test_scratch_script_named_in_the_first_read_after_the_start() -> None:
    """The lane started 1 s before the read is judged: no grace period, the same read names it."""
    drill = lane("drill-1", kind="drill", launched_from=SCRATCH_SCRIPT).model_copy(
        update={"started_at": request().now}
    )
    result = HandlerDispatchReading().handle(request((*clean_lanes(), drill)))

    assert [n.lane for n in result.lanes_without_record] == ["drill-1"]


def test_scratch_script_named_a_lane_with_no_record_and_no_scratch_source_is_named_apart() -> (
    None
):
    plain = lane("fill-c", launched_from="onex-remote-lane")
    unknown = lane("fill-d", launched_from=None)
    result = HandlerDispatchReading().handle(request((*clean_lanes(), plain, unknown)))

    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert {n.lane: n.from_scratch_script for n in result.lanes_without_record} == {
        "fill-c": False,
        "fill-d": False,
    }


def test_scratch_script_named_every_overlay_marker_names_a_scratch_source() -> None:
    from omnimarket.nodes.node_dispatch_reading_compute.tests.support import overlay

    for marker in overlay()["scratch_script_markers"]:
        drill = lane("drill-1", kind="drill", launched_from=f"bash {marker}x.sh")
        result = HandlerDispatchReading().handle(request((*clean_lanes(), drill)))

        assert result.lanes_without_record[0].from_scratch_script is True, marker


def test_scratch_script_named_a_lane_with_a_record_is_not_named_even_from_scratch() -> (
    None
):
    from omnimarket.nodes.node_dispatch_reading_compute.tests.support import record

    drill = lane("drill-1", kind="drill", launched_from=SCRATCH_SCRIPT)
    result = HandlerDispatchReading().handle(
        request(
            (*clean_lanes(), drill),
            (
                record("fill-a"),
                record("fill-b", terminal_after=None),
                record("drill-1"),
            ),
        )
    )

    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.lanes_without_record == ()
