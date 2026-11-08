# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22923, piece 17b: the customer health line is computed from the floor record's own piece readings.

Golden: fresh working readings give a WORKING line that carries the ages it used. Error: a reading older than its
own maximum age, a failing reading, an unread piece and a malformed request each end in a line that is not WORKING and
names the reading. ``test_injected_break_*`` runs the same assertions against a handler with one named break and
requires them to fail, so the reading is shown to be able to fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.floor_17a import ModelFloorHealthLine
from omnimarket.models.floor_17a.model_customer_health import (
    ModelCustomerHealthRequest,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_customer_health import (
    HandlerCustomerHealth,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 15, 30, tzinfo=UTC)
OVERLAY: dict[str, Any] = {
    "title": "Customer path",
    "required_pieces": [3, 11, 15, 18],
    "max_age_s": 900,
}
NAMES = {3: "Bus", 11: "Dev deploy", 15: "Delegation", 18: "Onboarding"}
FRESH_AGES = {3: 60, 11: 120, 15: 300, 18: 600}


def reading(piece: int, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "piece": piece,
        "name": NAMES.get(piece, f"Piece {piece}"),
        "reading_state": "WORKING",
        "reading_age_s": FRESH_AGES.get(piece, 60),
        "reading_max_age_s": 900,
    }
    row.update(changes)
    return row


def request(
    rows: list[dict[str, Any]], overlay: dict[str, Any] | None = None
) -> ModelCustomerHealthRequest:
    return ModelCustomerHealthRequest.model_validate(
        {
            "now": NOW,
            "readings": rows,
            "overlay": OVERLAY if overlay is None else overlay,
        }
    )


def all_fresh() -> list[dict[str, Any]]:
    return [reading(p) for p in (3, 11, 15, 18)]


class HandlerIgnoringAge(HandlerCustomerHealth):
    """The named break: a reading is never too old."""

    @staticmethod
    def _too_old(_age_s: int, _max_age_s: int) -> bool:
        return False


class HandlerTrustingState(HandlerCustomerHealth):
    """The named break: the line is the worst state a reading itself reports, whatever its age or absence."""

    def handle(self, request: ModelCustomerHealthRequest) -> ModelFloorHealthLine:
        line = super().handle(request)
        states = {r.reading_state for r in request.readings}
        worst = "NOT WORKING" if "NOT WORKING" in states else "WORKING"
        return line.model_copy(update={"state": worst})


# --- the assertions each break is run against ---------------------------------------------------------------------------


def check_golden(handler: HandlerCustomerHealth) -> None:
    line = handler.handle(request(all_fresh()))
    assert line.state == "WORKING"
    assert line.generated_at == NOW
    assert [(r.piece, r.age_s, r.max_age_s, r.verdict) for r in line.readings] == [
        (3, 60, 900, "fresh"),
        (11, 120, 900, "fresh"),
        (15, 300, 900, "fresh"),
        (18, 600, 900, "fresh"),
    ]
    assert line.oldest_age_s == 600
    assert line.named == ()
    assert line.line.startswith("Customer path: WORKING")
    assert "oldest reading 10 min" in line.line


def check_stale_reading(handler: HandlerCustomerHealth) -> None:
    rows = all_fresh()
    rows[2] = reading(15, reading_age_s=901)
    line = handler.handle(request(rows))
    assert line.state == "NOT WORKING"
    assert line.named == ("piece 15 Delegation: stale, read 15 min ago, limit 15 min",)
    stale = next(r for r in line.readings if r.piece == 15)
    assert (stale.verdict, stale.age_s, stale.max_age_s) == ("stale", 901, 900)
    assert "piece 15 Delegation" in line.line


# --- AC1: golden ------------------------------------------------------------------------------------------------------


def test_golden_fresh_working_readings_give_a_working_line_with_the_ages_it_used() -> (
    None
):
    check_golden(HandlerCustomerHealth())


def test_golden_only_the_overlays_required_pieces_count() -> None:
    rows = [*all_fresh(), reading(5, reading_state="NOT WORKING")]
    assert HandlerCustomerHealth().handle(request(rows)).state == "WORKING"
    narrow = {**OVERLAY, "required_pieces": [5]}
    line = HandlerCustomerHealth().handle(request(rows, narrow))
    assert line.state == "NOT WORKING"
    assert [r.piece for r in line.readings] == [5]


def test_golden_the_line_is_stamped_with_the_time_and_its_own_bound_from_the_overlay() -> (
    None
):
    line = HandlerCustomerHealth().handle(
        request(all_fresh(), {**OVERLAY, "max_age_s": 321})
    )
    assert (line.generated_at, line.max_age_s) == (NOW, 321)


def test_golden_a_reading_exactly_at_its_limit_is_still_fresh() -> None:
    rows = all_fresh()
    rows[0] = reading(3, reading_age_s=900)
    assert HandlerCustomerHealth().handle(request(rows)).state == "WORKING"


# --- AC2: error, a reading older than its limit ------------------------------------------------------------------------


def test_stale_reading_makes_the_line_not_working_and_names_the_reading() -> None:
    check_stale_reading(HandlerCustomerHealth())


def test_stale_reading_wins_over_the_state_the_old_reading_still_carries() -> None:
    rows = all_fresh()
    rows[3] = reading(18, reading_state="WORKING", reading_age_s=7200)
    line = HandlerCustomerHealth().handle(request(rows))
    assert line.state == "NOT WORKING"
    assert line.named[0].startswith("piece 18 Onboarding: stale, read 120 min ago")


def test_stale_reading_each_reading_is_judged_against_its_own_limit() -> None:
    rows = all_fresh()
    rows[0] = reading(3, reading_age_s=100, reading_max_age_s=60)
    line = HandlerCustomerHealth().handle(request(rows))
    assert line.state == "NOT WORKING"
    assert line.named == ("piece 3 Bus: stale, read 2 min ago, limit 1 min",)


def test_error_a_failing_reading_is_not_working_and_named() -> None:
    rows = all_fresh()
    rows[1] = reading(11, reading_state="NOT WORKING")
    line = HandlerCustomerHealth().handle(request(rows))
    assert line.state == "NOT WORKING"
    assert line.named == ("piece 11 Dev deploy: reading NOT WORKING",)


def test_error_a_reading_dated_in_the_future_is_not_working() -> None:
    rows = all_fresh()
    rows[0] = reading(3, reading_age_s=-30)
    line = HandlerCustomerHealth().handle(request(rows))
    assert line.state == "NOT WORKING"
    assert "future" in line.named[0]


def test_error_a_piece_with_no_reading_row_is_unknown_never_working() -> None:
    line = HandlerCustomerHealth().handle(request(all_fresh()[:3]))
    assert line.state == "UNKNOWN"
    assert line.named == ("piece 18: no reading",)
    assert [r.verdict for r in line.readings][-1] == "unread"


def test_error_a_reading_with_no_age_is_unread() -> None:
    rows = all_fresh()
    rows[2] = reading(15, reading_state=None, reading_age_s=None)
    line = HandlerCustomerHealth().handle(request(rows))
    assert line.state == "UNKNOWN"
    assert line.named == ("piece 15 Delegation: not read",)


def test_error_not_working_outranks_unknown() -> None:
    rows = all_fresh()[:3]
    rows[0] = reading(3, reading_state="NOT WORKING")
    assert HandlerCustomerHealth().handle(request(rows)).state == "NOT WORKING"


def test_error_a_malformed_request_is_refused_not_answered_working() -> None:
    for bad in (
        {**OVERLAY, "required_pieces": []},
        {**OVERLAY, "required_pieces": [3, 3]},
        {**OVERLAY, "max_age_s": 0},
        {**OVERLAY, "title": ""},
        {**OVERLAY, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            request(all_fresh(), bad)
    with pytest.raises(ValidationError):
        request([reading(3, reading_state="MAYBE")])


# --- the reading can fail: each named break is caught by the same assertions -------------------------------------------


def test_injected_break_ignoring_age_is_caught_by_the_stale_reading_check() -> None:
    check_stale_reading(HandlerCustomerHealth())
    with pytest.raises(AssertionError):
        check_stale_reading(HandlerIgnoringAge())


def test_injected_break_trusting_the_readings_state_is_caught_by_the_stale_reading_check() -> (
    None
):
    with pytest.raises(AssertionError):
        check_stale_reading(HandlerTrustingState())
    check_golden(HandlerTrustingState())


def test_injected_break_ignoring_age_still_passes_the_golden_check() -> None:
    # The golden case alone cannot see this break; the stale reading check is what makes the reading able to fail.
    check_golden(HandlerIgnoringAge())
