# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A TERMINAL that is not done is reported as filtered, not as a silent zero (OMN-26520).

On the dev runtime 179 of the last 200 work-ledger TERMINAL events carried an
outcome other than done, so the writer legitimately wrote nothing for most of the
topic, and the runtime read each zero as a writer that wrote nothing. The writer
now says so in its result (``events_not_applicable``) so the runtime can tell a
filter from a fault. The fixture is the recorded dev event shape.

- test_not_done_terminal_reports_not_applicable: fails at origin/dev (no key).
- test_done_terminal_reports_a_write_and_no_filter: the done path is unchanged.
- test_replayed_done_event_is_not_reported_as_filtered: a zero the writer cannot
  explain by its filter stays a plain zero.
- test_malformed_event_still_raises: an error is never reported as a filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
)
from tests.nodes.node_projection_done_items.test_done_items_projection import (
    V1,
    Served,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dev_work_ledger_terminal_events_2026-11-23.json"
)
NOT_APPLICABLE = "events_not_applicable"


def _event(name: str) -> dict[str, Any]:
    recorded: dict[str, Any] = json.loads(FIXTURE.read_text())[name]
    return recorded


def _handle(
    writer: DoneItemsProjectionWriter, payload: dict[str, Any], offset: int
) -> dict[str, Any]:
    result: dict[str, Any] = writer.handle(
        {**payload, "_topic": V1, "_partition": 0, "_offset": offset}
    )
    return result


def test_not_done_terminal_reports_not_applicable(served: Served) -> None:
    writer, _ = served
    result = _handle(writer, _event("handed_off"), 0)
    assert result["rows_upserted"] == 0
    assert result[NOT_APPLICABLE] == 1


def test_done_terminal_reports_a_write_and_no_filter(served: Served) -> None:
    writer, _ = served
    result = _handle(writer, _event("done"), 0)
    assert result["rows_upserted"] == 1
    assert result.get(NOT_APPLICABLE, 0) == 0


def test_replayed_done_event_is_not_reported_as_filtered(served: Served) -> None:
    writer, _ = served
    _handle(writer, _event("done"), 0)
    replay = _handle(writer, _event("done"), 1)
    assert replay["rows_upserted"] == 0
    assert replay.get(NOT_APPLICABLE, 0) == 0


def test_malformed_event_still_raises(served: Served) -> None:
    writer, _ = served
    with pytest.raises(ValidationError):
        _handle(writer, {"row_type": "TERMINAL", "ledger_id": "x"}, 0)
