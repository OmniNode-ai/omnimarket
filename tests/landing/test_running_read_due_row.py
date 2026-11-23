# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-26491): a merged runtime-affecting pull request writes one running-read due row.

The row is due at merge plus 60 minutes, carries the ticket and the reading the pull request body names
(node and topic, unit and host, or overlay and reader). The falsifier: a merged fixture pull request that
produces no due row fails this file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.events.running_read_due import (
    ModelMergedPrFact,
    ModelRunningReadDueRequest,
)
from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_running_read_due_compute.handlers.handler_running_read_due import (
    HandlerRunningReadDue,
)

pytestmark = pytest.mark.unit

MERGED_AT = datetime(2026, 11, 23, 0, 30, 0, tzinfo=UTC)


def _merged(
    body: str, *, runtime_affecting: bool = True, ticket: str | None = "OMN-26491"
) -> ModelMergedPrFact:
    return ModelMergedPrFact(
        pr="OmniNode-ai/omnimarket#6700",
        ticket=ticket,
        owner="orchestrator-31d9-swarm",
        merged_at=MERGED_AT,
        runtime_affecting=runtime_affecting,
        pr_body=body,
    )


def _write(merged: ModelMergedPrFact):
    return HandlerRunningReadDue().handle(
        ModelRunningReadDueRequest(operation="write_due_row", merged=merged)
    )


def test_merged_runtime_pr_writes_one_due_row_at_merge_plus_sixty_minutes() -> None:
    result = _write(
        _merged(
            "Fixes the tick.\nRunning read: node=node_floor_fill_tick topic=onex.evt.floor.fill.v1\n"
        )
    )

    assert result.append_request is not None, (
        "a merged runtime-affecting PR produced no due row"
    )
    rows = result.append_request.rows.splitlines()
    assert len(rows) == 1
    row = grammar.parse_row(rows[0])
    assert row is not None
    assert row.row_type == "STATUS"
    assert row.stamp == "2026-11-23T00:30:00Z"
    assert row.value("due") == "2026-11-23T01:30:00Z"
    assert row.value("ticket") == "OMN-26491"
    assert (
        row.value("reading") == "node:node_floor_fill_tick,topic:onex.evt.floor.fill.v1"
    )
    assert row.value("owner") == "orchestrator-31d9-swarm"
    assert row.value("pr") == "OmniNode-ai/omnimarket#6700"
    assert row.value("kind") == "running-read-due"
    assert grammar.row_refusals(rows[0]) == []
    # One row per merge: the id is the pull request's, so a replayed merge appends nothing new.
    assert result.append_request.append_if_absent_row_id == result.due_id
    assert row.value("id") == result.due_id


@pytest.mark.parametrize(
    ("body", "reading"),
    [
        (
            "Running read: unit=onex-floor-fill-tick host=h051",
            "unit:onex-floor-fill-tick,host:h051",
        ),
        (
            "Running read: overlay=example-overlay reader=example-reader",
            "overlay:example-overlay,reader:example-reader",
        ),
    ],
)
def test_reading_forms_unit_host_and_overlay_reader(body: str, reading: str) -> None:
    result = _write(_merged(body))
    assert result.append_request is not None
    row = grammar.parse_row(result.append_request.rows)
    assert row is not None
    assert row.value("reading") == reading


def test_a_body_naming_no_reading_still_writes_the_row_as_unnamed() -> None:
    result = _write(_merged("No reading named here."))
    assert result.append_request is not None
    row = grammar.parse_row(result.append_request.rows)
    assert row is not None
    assert row.value("reading") == "unnamed"


def test_a_pipe_in_the_reading_never_forges_a_cell() -> None:
    result = _write(_merged("Running read: node=a|b topic=t"))
    assert result.append_request is not None
    assert len(result.append_request.rows.splitlines()) == 1
    assert "reading=unnamed" in result.append_request.rows


def test_a_merge_that_is_not_runtime_affecting_writes_no_row() -> None:
    result = _write(_merged("Running read: node=n topic=t", runtime_affecting=False))
    assert result.append_request is None


def test_a_merge_with_no_ticket_is_refused_not_written() -> None:
    result = _write(_merged("Running read: node=n topic=t", ticket=None))
    assert result.append_request is None
    assert result.refusal is not None
