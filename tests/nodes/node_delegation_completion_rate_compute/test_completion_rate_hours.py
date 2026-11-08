# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The completion-rate reading over planted hours (OMN-23108): below the bound reads red, above it reads green.

The bounds come from the overlay file, never from code: the last tests change the overlay and the same rows read the other way.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_completion_rate_compute.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRate,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    DEPLOYED,
    HARNESS,
    above_bound_hour,
    below_bound_hour,
    event,
    hour,
    overlay,
    overlay_data,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit


def read(**over: Any) -> ModelDelegationCompletionRate:
    reading: ModelDelegationCompletionRate = resolve_handler()().handle(request(**over))
    return reading


def test_an_above_bound_hour_reads_green_with_its_numbers() -> None:
    reading = read(events=above_bound_hour())
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.r_all, reading.r_dep, reading.failed_rate) == (136, 100, 0.0)
    assert reading.rows_in_window == 136


def test_a_below_bound_hour_reads_red_and_names_every_bound_it_breaks() -> None:
    reading = read(events=below_bound_hour())
    assert reading.verdict is V.NOT_WORKING
    assert (reading.r_all, reading.r_dep) == (14, 0)
    assert reading.failed_rate == pytest.approx(5 / 19)
    assert reading.reasons == (
        "r_all-below-bound:14<20",
        "r_dep-below-bound:0<10",
        "failed_rate-above-bound:0.2632>0.2000",
    )


def test_a_dead_deployed_lane_reads_red_though_r_all_alone_passes() -> None:
    rows = tuple(event(1 + i * 0.4, backend=HARNESS) for i in range(94))
    reading = read(events=rows)
    assert reading.r_all == 94
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == ("r_dep-below-bound:0<10",)


def test_an_empty_window_reads_red_never_a_zero_that_passes() -> None:
    reading = read(events=())
    assert reading.verdict is V.NOT_WORKING
    assert (reading.r_all, reading.r_dep, reading.failed_rate) == (0, 0, None)
    assert reading.reasons == ("r_all-below-bound:0<20", "r_dep-below-bound:0<10")


def test_rows_that_could_not_be_read_read_unknown_and_never_zero() -> None:
    reading = read(events=(), events_unread_reason="delegation_events down")
    assert reading.verdict is V.UNKNOWN
    assert reading.r_all is None
    assert reading.r_dep is None
    assert reading.reasons == ("events-unreadable:delegation_events-down",)
    assert "outcome=source-unreadable" in reading.ledger_row


def test_only_real_non_shadow_rows_that_passed_the_gate_complete() -> None:
    rows = (
        *above_bound_hour(),
        event(5, source="synthetic"),
        event(5, shadow=True),
        event(5, gate=False),
        event(5, gate=None),
    )
    reading = read(events=rows)
    assert reading.r_all == 136
    assert reading.r_dep == 100
    # The gate-failed rows ended terminal_ok, so they are not failures; they just do not complete.
    assert reading.failed_rate == 0.0


def test_a_row_with_no_answering_backend_is_not_a_deployed_answer() -> None:
    reading = read(events=(*above_bound_hour(), event(5, backend=None)))
    assert (reading.r_all, reading.r_dep) == (137, 100)


def test_the_window_is_the_overlays_and_its_edges_are_exact() -> None:
    inside = event(59.99)
    edge = event(60)
    older = event(61)
    future = event(-1)
    reading = read(events=(inside, edge, older, future))
    assert reading.rows_in_window == 1
    assert reading.rows_future_dated == 1
    wider = read(
        overlay=overlay(window_seconds=7200), events=(inside, edge, older, future)
    )
    assert wider.rows_in_window == 3


def test_the_bound_is_the_overlays_not_the_codes() -> None:
    """The same rows read the other way when the overlay's bounds move: no bound is in the handler."""
    bounds = overlay_data()["bounds"]
    loose = {**bounds, "min_r_all": 10, "min_r_dep": 5}
    strict = {**bounds, "min_r_all": 500}
    thin = hour(5, 9)
    assert read(events=thin).verdict is V.NOT_WORKING
    assert read(events=thin, overlay=overlay(bounds=loose)).verdict is V.WORKING
    assert read(events=above_bound_hour()).verdict is V.WORKING
    assert (
        read(events=above_bound_hour(), overlay=overlay(bounds=strict)).verdict
        is V.NOT_WORKING
    )


def test_an_overlay_missing_a_bound_is_refused_not_defaulted() -> None:
    data = overlay_data()
    del data["bounds"]["max_failed_rate"]
    with pytest.raises(ValidationError):
        type(overlay()).model_validate(data)
    data = overlay_data()
    data["window"] = 3600
    with pytest.raises(ValidationError):
        type(overlay()).model_validate(data)


def test_the_ledger_row_carries_the_reading_in_the_cells_the_floor_declares() -> None:
    reading = read(events=below_bound_hour())
    cells = dict(c.split("=", 1) for c in reading.ledger_row.split(" | ")[2:-1])
    assert reading.ledger_row.split(" | ")[1] == "STATUS"
    assert cells["lane"] == "delegation-rate"
    assert cells["src"] == "delegation-rate"
    assert cells["drill"] == "no"
    assert cells["outcome"] == "read"
    assert cells["verdict"] == "NOT_WORKING"
    assert (cells["r_all"], cells["r_dep"], cells["failed_rate"]) == (
        "14",
        "0",
        "0.2632",
    )
    assert cells["max_age"] == "7200"
    assert cells["published_at"] == "2026-11-08T07:00:00Z"
    assert DEPLOYED not in reading.ledger_row
