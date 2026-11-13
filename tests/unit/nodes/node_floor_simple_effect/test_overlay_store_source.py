# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's overlay store row: a stale or storeless read is not ok; a missing record is unread, never blank."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from omnimarket.handlers.floor_projection import ProjectionPayload
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelLabSourceDef,
    ModelLabStateOverlay,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    overlay_store_events,
    read_overlay_store,
)

pytestmark = pytest.mark.unit


NOW = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


class _Overlay:
    sources = (
        ModelLabSourceDef(
            id="overlay_store",
            title="Overlay store",
            kind="overlay_store",
            max_age_s=900,
        ),
    )


OVERLAY = cast(ModelLabStateOverlay, _Overlay())


def _fleet(readings: dict[str, dict[str, object]]) -> ProjectionPayload:
    """The overlay store fleet as the bus projection carries it, fresh."""
    payload: dict[str, Any] = {"readings": readings}
    return ProjectionPayload("overlay_store_fleet", payload, "", as_of=NOW, age_s=5)


def _unknown(reason: str) -> ProjectionPayload:
    return ProjectionPayload("overlay_store_fleet", None, reason)


def _reading(state: str, *, store_error: str | None = None) -> dict[str, object]:
    return {
        "state": state,
        "age_s": 10.0,
        "store_error": store_error,
        "detail": "",
        "read_at": "2026-11-06T01:00:00Z",
    }


def test_each_host_is_a_subject_and_only_fresh_with_an_answering_store_is_ok() -> None:
    read = read_overlay_store(
        _fleet(
            {
                "h1:lab_hosts": _reading("fresh"),
                "h2:lab_hosts": _reading("stale"),
                "h3:lab_hosts": _reading("fresh", store_error="broker refused"),
                "h4:lab_hosts": _reading("unreadable"),
            },
        ),
        OVERLAY,
        NOW,
    )
    assert read.status == "read"
    assert [(s.name, s.ok) for s in read.subjects] == [
        ("h1:lab_hosts", True),
        ("h2:lab_hosts", False),
        ("h3:lab_hosts", False),
        ("h4:lab_hosts", False),
    ]
    assert read.subjects[2].note == "broker refused"
    assert read.summary == "1 of 4 overlay reads fresh"


def test_a_missing_record_is_unread() -> None:
    read = read_overlay_store(
        _unknown("the projection reads were not written"), OVERLAY, NOW
    )
    assert read.status == "unread"
    assert "the projection reads were not written" in read.reason


def test_a_record_with_no_readings_is_unread() -> None:
    read = read_overlay_store(_fleet({}), OVERLAY, NOW)
    assert read.status == "unread"


def test_a_fresh_row_is_a_golden_chain() -> None:
    read = read_overlay_store(
        _fleet({"h1:lab_hosts": _reading("fresh")}),
        OVERLAY,
        NOW,
    )
    events = overlay_store_events(read, 17, NOW, "c1")
    assert [e.phase for e in events] == ["start", "success"]
    assert events[1].counters == {"reads_fresh": 1}


def test_a_stale_row_and_a_missing_record_are_typed_error_chains() -> None:
    stale = read_overlay_store(
        _fleet({"h1:lab_hosts": _reading("stale")}),
        OVERLAY,
        NOW,
    )
    events = overlay_store_events(stale, 17, NOW, "c2")
    assert [e.phase for e in events] == ["start", "failure"]
    assert events[1].failure_code == "overlay_store_not_fresh"
    assert events[1].failure_detail == "h1:lab_hosts"
    unread = read_overlay_store(
        _unknown("the projection reads were not written"), OVERLAY, NOW
    )
    assert overlay_store_events(unread, 17, NOW, "c3")[1].failure_code == (
        "overlay_store_unread"
    )


def test_the_deployed_receipt_fold_counts_both_chains_for_the_piece() -> None:
    fresh = read_overlay_store(
        _fleet({"h1:lab_hosts": _reading("fresh")}),
        OVERLAY,
        NOW,
    )
    unread = read_overlay_store(
        _unknown("the projection reads were not written"), OVERLAY, NOW
    )
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=(
                *overlay_store_events(fresh, 17, NOW, "c4"),
                *overlay_store_events(unread, 17, NOW, "c5"),
            ),
            pieces=(ModelPieceDef(piece=17, name="floor", reading_max_age_s=900),),
            joins=(),
            freshness_max_age_s=900,
            error_max_age_s=86400,
            chain_deadline_s=600,
        )
    )
    assert receipts.pieces[0].golden_at == NOW
    assert receipts.pieces[0].error_at == NOW
    assert receipts.findings == ()


def test_a_host_whose_reader_stopped_reads_not_ok_though_its_last_reading_said_fresh() -> (
    None
):
    old = _reading("fresh")
    old["read_at"] = "2026-11-06T00:30:00Z"
    read = read_overlay_store(
        _fleet({"h1:lab_hosts": _reading("fresh"), "h2:lab_hosts": old}),
        OVERLAY,
        NOW,
    )
    assert [(s.name, s.ok) for s in read.subjects] == [
        ("h1:lab_hosts", True),
        ("h2:lab_hosts", False),
    ]
    assert read.subjects[1].note == "no reading for 1800s, over the 900s bound"
    assert overlay_store_events(read, 17, NOW, "c6")[1].failure_detail == "h2:lab_hosts"


def test_a_projection_older_than_its_bound_is_unread_not_the_fleet_it_last_held() -> (
    None
):
    """The producers stopped: the projection reads unknown, so the row is unread and never blank or all-stale hosts."""
    from omnimarket.handlers.floor_projection import projection_payload
    from omnimarket.models.floor_projection import (
        ModelFloorProjectionBundle,
        ModelFloorProjectionEntry,
    )

    bundle = ModelFloorProjectionBundle(
        read_at=NOW,
        entries={
            "overlay_store_fleet": ModelFloorProjectionEntry(
                projection_id="overlay_store_fleet",
                status="read",
                max_age_s=900,
                read_at=NOW,
                as_of=NOW - timedelta(hours=2),
                producer="h1",
                payload={"readings": {"h1:lab_hosts": _reading("fresh")}},
            )
        },
    )
    read = read_overlay_store(
        projection_payload(bundle, "overlay_store_fleet", NOW), OVERLAY, NOW
    )
    assert read.status == "unread"
    assert "over its 900s bound" in read.reason
    events = overlay_store_events(read, 17, NOW, "c7")
    assert events[1].failure_code == "overlay_store_unread"


def test_a_payload_that_is_not_a_fleet_cannot_be_parsed_and_is_unread() -> None:
    read = read_overlay_store(
        ProjectionPayload("overlay_store_fleet", {"readings": ["h1"]}, "", as_of=NOW),
        OVERLAY,
        NOW,
    )
    assert read.status == "unread"
    assert "cannot be parsed" in read.reason
