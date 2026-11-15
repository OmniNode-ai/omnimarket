# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read's budget (OMN-24521).

Route: a read timeout or a total over the schema ceilings is refused, so is a read
timeout over the total and so is a route with no total at all. Budget: a handler whose
monotonic spends the total reads the rows it reaches, handing each the budget it had
left, and the rows it does not reach read UNREAD with the ``budget was spent`` detail,
each still emitting its refused event, never dropped and never a pass; a fast monotonic
reads every row. Port: the command port runs its runner under the lesser of the route's
own timeout and the caller's budget. Seam: both ceilings sit under the facts effect's
30000 ms ``descriptor.timeout_ms``, and the shipped example overlay validates as a route.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    ModelRulingExpect,
    ModelRulingRead,
    ModelRulingReading,
    ModelRulingReadRequest,
    ModelRulingRegisterPolicy,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_effect.adapters import (
    CommandRulingReadPort,
    InMemoryRulingSink,
)
from omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read import (
    HandlerRulingRegisterRead,
    load_topics,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    MAX_READ_TIMEOUT_SECONDS,
    MAX_TOTAL_TIMEOUT_SECONDS,
    ModelRulingRoute,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
TOPICS = load_topics()


def route(**changes: object) -> ModelRulingRoute:
    """A route under the ceilings: ``local`` runs directly, ``host-a`` over ssh."""
    fields: dict[str, object] = {
        "kind": "command",
        "timeout_seconds": 10,
        "total_timeout_seconds": 20,
        "remote_argv_prefix": ("ssh", "-o", "BatchMode=yes", "{target}"),
        "local_hosts": ("local",),
        "host_targets": {"host-a": "192.0.2.10"},
        "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
    }
    fields.update(changes)
    return ModelRulingRoute.model_validate(fields)


def row(row_id: str = "row-1") -> ModelRulingRow:
    return ModelRulingRow(
        row_id=row_id,
        ruling="2026-07-01T10:00:00Z",
        must_be_true="the example ruling holds",
        read=ModelRulingRead(
            host="local",
            argv=("example-command",),
            ok_exit_codes=(0, 1),
            expect=ModelRulingExpect(kind="exit_zero"),
        ),
        owner="example-lane",
        fix=f"fix {row_id}",
    )


def policy(*rows: ModelRulingRow) -> ModelRulingRegisterPolicy:
    return ModelRulingRegisterPolicy(
        max_reading_age_seconds=600,
        rows=rows,
        allowed_commands=("example-command",),
    )


# ---- the route's ceilings -------------------------------------------------------------------------


def test_a_read_timeout_over_its_ceiling_is_refused() -> None:
    with pytest.raises(
        ValidationError, match=f"less than or equal to {MAX_READ_TIMEOUT_SECONDS}"
    ):
        route(timeout_seconds=MAX_READ_TIMEOUT_SECONDS + 1)


def test_a_total_timeout_over_its_ceiling_is_refused() -> None:
    with pytest.raises(
        ValidationError, match=f"less than or equal to {MAX_TOTAL_TIMEOUT_SECONDS}"
    ):
        route(total_timeout_seconds=MAX_TOTAL_TIMEOUT_SECONDS + 1)


def test_a_read_timeout_over_the_total_is_refused() -> None:
    with pytest.raises(ValidationError, match="exceeds the register's total"):
        route(timeout_seconds=10, total_timeout_seconds=5)


def test_a_route_without_the_total_key_is_refused() -> None:
    fields: dict[str, object] = {
        "kind": "inmemory",
        "timeout_seconds": 10,
        "remote_argv_prefix": (),
        "local_hosts": ("local",),
        "host_targets": {},
        "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
    }
    with pytest.raises(ValidationError, match="total_timeout_seconds"):
        ModelRulingRoute.model_validate(fields)


# ---- the handler's whole-read budget --------------------------------------------------------------


class SteppingPort:
    """A port that records the budget it was handed and spends a fixed slice of the
    injected monotonic per read, so a slow host is a step of the clock."""

    def __init__(self, elapsed: list[float], seconds_per_read: float) -> None:
        self._elapsed = elapsed
        self._seconds_per_read = seconds_per_read
        self.budgets: list[int | None] = []

    def read(
        self, row: ModelRulingRow, budget_seconds: int | None = None
    ) -> ModelRulingReading:
        self.budgets.append(budget_seconds)
        self._elapsed.append(self._elapsed[-1] + self._seconds_per_read)
        return ModelRulingReading(
            row_id=row.row_id,
            status=EnumReadStatus.OK,
            exit_code=0,
            stdout="",
            read_at=NOW,
        )


def budget_handler(
    port: SteppingPort, elapsed: list[float], total: int
) -> tuple[HandlerRulingRegisterRead, InMemoryRulingSink]:
    sink = InMemoryRulingSink()

    def monotonic() -> float:
        return elapsed[-1]

    return (
        HandlerRulingRegisterRead(
            port,
            sink,
            TOPICS,
            total_timeout_seconds=total,
            clock=lambda: NOW,
            monotonic=monotonic,
        ),
        sink,
    )


def test_the_handler_reads_only_the_rows_the_budget_reaches() -> None:
    """Each read spends ten seconds of the total's twenty-five: rows 0-2 read with the
    budget each had left (25, 15, 5), rows 3-4 are past the budget and read UNREAD
    with the spent detail, and every row still emits its event."""
    elapsed = [0.0]
    port = SteppingPort(elapsed, seconds_per_read=10.0)
    h, sink = budget_handler(port, elapsed, total=25)
    rows = tuple(row(f"row-{index}") for index in range(5))
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert port.budgets == [25, 15, 5]
    assert len(result.readings) == 5
    assert [reading.row_id for reading in result.readings] == [
        row_i.row_id for row_i in rows
    ]
    assert [reading.status for reading in result.readings] == [
        EnumReadStatus.OK,
        EnumReadStatus.OK,
        EnumReadStatus.OK,
        EnumReadStatus.UNREAD,
        EnumReadStatus.UNREAD,
    ]
    spent = "not read: the register's 25 second read budget was spent"
    for reading in result.readings[3:]:
        assert reading.detail == spent
        assert reading.read_at == NOW
        assert reading.exit_code is None
    assert len(sink.events) == 5
    assert [event.kind for event in sink.events] == [
        "answered",
        "answered",
        "answered",
        "refused",
        "refused",
    ]
    for event, row_i in zip(sink.events[3:], rows[3:], strict=True):
        assert event.row_id == row_i.row_id
        assert event.topic == TOPICS["ruling_read_refused"]
        assert event.status is EnumReadStatus.UNREAD
        assert event.detail == spent


def test_a_fast_monotonic_reads_every_row() -> None:
    """Positive control: the same five rows with a monotonic that does not move all
    read, each handed the full total, and every event is answered."""
    elapsed = [0.0]
    port = SteppingPort(elapsed, seconds_per_read=0.0)
    h, sink = budget_handler(port, elapsed, total=25)
    rows = tuple(row(f"row-{index}") for index in range(5))
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert port.budgets == [25, 25, 25, 25, 25]
    assert [reading.status for reading in result.readings] == [EnumReadStatus.OK] * 5
    assert [event.kind for event in sink.events] == ["answered"] * 5
    assert sink.events[0].row_id == "row-0"
    assert sink.events[-1].row_id == "row-4"


# ---- the command port's budget --------------------------------------------------------------------


def test_the_command_port_runs_its_runner_under_the_lesser_of_route_and_budget() -> (
    None
):
    timeouts: list[int] = []

    def recording_runner(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
        timeouts.append(timeout_seconds)
        return 0, ""

    port = CommandRulingReadPort(route(), runner=recording_runner, clock=lambda: NOW)
    read = port.read(row("row-budget"), budget_seconds=3)
    assert read.status is EnumReadStatus.OK
    assert timeouts == [3]
    timeouts.clear()
    assert port.read(row("row-budget")).status is EnumReadStatus.OK
    assert timeouts == [10]
    timeouts.clear()
    assert port.read(row("row-budget"), budget_seconds=None).status is EnumReadStatus.OK
    assert timeouts == [10]


# ---- the seam -------------------------------------------------------------------------------------


def test_the_budget_ceilings_sit_under_the_facts_effect_timeout() -> None:
    """The ceilings are schema constants, not deployment values; the seam that must
    hold them is the facts effect's own ``descriptor.timeout_ms``."""
    contract = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_throughput_tick_facts_effect/contract.yaml"
    )
    parsed = yaml.safe_load(contract.read_text(encoding="utf-8"))
    timeout_ms = parsed["descriptor"]["timeout_ms"]
    assert timeout_ms > MAX_READ_TIMEOUT_SECONDS * 1000
    assert timeout_ms > MAX_TOTAL_TIMEOUT_SECONDS * 1000


def test_the_shipped_example_overlay_validates_as_a_route() -> None:
    example = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_ruling_register_effect/overlay.example.yaml"
    )
    loaded = ModelRulingRoute.model_validate(
        yaml.safe_load(example.read_text(encoding="utf-8"))
    )
    assert loaded.total_timeout_seconds >= loaded.timeout_seconds
