# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A projection failure surfaces with its real cause, never chained to "no running event loop" (OMN-24634).

``recorded_unfixed_run_traceback.txt`` is the traceback the unfixed ``_run`` produced for an
UnsequencedLiveRowError, reproduced locally from origin/dev (the dev runtime's own copies rolled off
its log at a restart): the real error is raised while the ``RuntimeError: no running event loop`` probe
is still being handled, so it prints as its chained context.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

import pytest

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    UnsequencedLiveRowError,
    WorkLedgerProjectionWriter,
)
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_writer import (
    _FakeDb,
    _writer,
)

pytestmark = pytest.mark.unit

RECORDED = (Path(__file__).parent / "recorded_unfixed_run_traceback.txt").read_text()
LOOP_MESSAGE = "no running event loop"


def test_event_loop_the_recorded_traceback_shows_the_chain_the_fix_removes() -> None:
    assert LOOP_MESSAGE in RECORDED
    assert "During handling of the above exception" in RECORDED
    assert (
        RECORDED.rstrip().endswith("requires a ledger_seq; row_id deadbeef")
        or "UnsequencedLiveRowError" in RECORDED.splitlines()[-1]
    )


def test_event_loop_a_failure_in_run_carries_no_loop_context() -> None:
    async def boom() -> None:
        raise UnsequencedLiveRowError("real cause")

    with pytest.raises(UnsequencedLiveRowError) as exc_info:
        WorkLedgerProjectionWriter._run(boom())
    err = exc_info.value
    assert err.__context__ is None
    assert err.__cause__ is None
    text = "".join(traceback.format_exception(err))
    assert LOOP_MESSAGE not in text
    assert "During handling of the above exception" not in text


def test_event_loop_a_failure_inside_a_running_loop_carries_no_loop_context() -> None:
    import asyncio

    async def boom() -> None:
        raise UnsequencedLiveRowError("real cause")

    async def outer() -> None:
        WorkLedgerProjectionWriter._run(boom())

    with pytest.raises(UnsequencedLiveRowError) as exc_info:
        asyncio.run(outer())
    assert LOOP_MESSAGE not in "".join(traceback.format_exception(exc_info.value))


def test_failure_logged_with_its_real_cause_as_the_top_level_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _writer(_FakeDb())
    writer._config = writer._config.model_copy(update={"sequence_assigned_sources": ()})
    data: dict[str, Any] = {
        "raw_row": "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
        "_topic": EnumLedgerRowType.CLAIM.topic,
        "source": "onex-ledger",
        "ledger_seq": None,
    }
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(UnsequencedLiveRowError) as exc_info,
    ):
        writer.handle(data)
    assert exc_info.value.__context__ is None
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1
    assert "UnsequencedLiveRowError" in records[0].getMessage()
    assert "unsequenced-live-row" in records[0].getMessage()
    assert LOOP_MESSAGE not in records[0].getMessage()
    assert records[0].exc_info is not None
    assert LOOP_MESSAGE not in "".join(traceback.format_exception(*records[0].exc_info))
