# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader and request findings of the OMN-22888 review: an impossible stamp, a lane-less row and a naive as_of."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_owner_count_compute.models import (
    ModelFloorFillOwnerCountRequest,
)
from omnimarket.nodes.node_floor_fill_owner_count_compute.protocols.protocol_lane_write_reader import (
    parse_ledger_text,
)

pytestmark = pytest.mark.unit

_UNTIL = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
_SINCE = _UNTIL - timedelta(hours=1)


def test_an_impossible_calendar_stamp_is_a_skipped_line_not_a_crash() -> None:
    text = (
        "2026-02-30T11:30:00Z | CLAIM | lane=a | x\n"
        "2026-11-07T11:30:00Z | CLAIM | lane=b | x\n"
    )
    writes = parse_ledger_text(text, _SINCE, _UNTIL)
    assert writes.lines_skipped == 1
    assert [row.lane for row in writes.rows] == ["b"]


def test_an_in_window_row_with_no_lane_is_skipped_and_counted() -> None:
    text = (
        "2026-11-07T11:30:00Z | CLAIM | lane= | x\n"
        "2026-11-07T11:31:00Z | CLAIM | ticket=OMN-1 | x\n"
        "2026-11-07T11:32:00Z | CLAIM | lane=b | x\n"
    )
    writes = parse_ledger_text(text, _SINCE, _UNTIL)
    assert writes.lines_read == 1
    assert writes.lines_skipped == 2
    assert writes.lines_read == len(writes.rows)


def test_a_naive_as_of_is_refused() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ModelFloorFillOwnerCountRequest(as_of=datetime(2026, 11, 7, 12, 0, 0))


def test_an_aware_as_of_is_accepted() -> None:
    assert ModelFloorFillOwnerCountRequest(as_of=_UNTIL).as_of == _UNTIL
    assert ModelFloorFillOwnerCountRequest().as_of is None
