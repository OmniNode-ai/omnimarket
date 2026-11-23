# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26435: drives PostgresFreshnessReader.read_coverage over a fake connection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.ledger_row_parser import row_id_of
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    ModelCoverageCheck,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
CHECK = ModelCoverageCheck(
    check_id="done_items_coverage",
    demand_relation="omninode_internal.work_ledger_rows",
    supply_relation="omninode_internal.done_items",
    max_age_seconds=1234,
    lookback_seconds=5678,
)


def _row(ticket: str, outcome: str, age: int) -> str:
    stamp = (NOW - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | TERMINAL | lane=alpha | ticket={ticket} | outcome={outcome} | finished"


def _supply(raw: str, ticket: str) -> tuple:
    return (ticket, raw.split(" | ")[0], row_id_of(raw))


class _Cursor:
    def __init__(self, executed: list, results: list) -> None:
        self._executed = executed
        self._results = results

    def execute(self, sql: str, params: tuple) -> None:
        self._executed.append((sql, params))

    def fetchall(self) -> list:
        return self._results.pop(0)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Conn:
    def __init__(self, results: list, executed: list) -> None:
        self._results = results
        self._executed = executed
        self.closed = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self._executed, self._results)

    def close(self) -> None:
        self.closed = 1


def _read(demand_rows: list, supply_rows: list):
    executed: list = []
    results = [list(demand_rows), list(supply_rows)]
    reader = PostgresFreshnessReader(dsn="postgresql://unused", now=lambda: NOW)
    reader._conn = _Conn(results, executed)
    observation = reader.read_coverage(CHECK)
    return observation, executed


def test_reader_counts_an_unprojected_done_event():
    raw = _row("OMN-1", "done", 3600)
    observation, _ = _read([(raw,)], [])
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 1
    assert observation.refused_rows == 0


def test_reader_counts_a_projected_done_event_as_covered():
    raw = _row("OMN-1", "done", 3600)
    observation, _ = _read([(raw,)], [_supply(raw, "OMN-1")])
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 0


def test_reader_counts_an_older_supply_row_as_unprojected():
    older = _row("OMN-1", "done", 7200)
    newer = _row("OMN-1", "done", 3600)
    observation, _ = _read([(older,), (newer,)], [_supply(older, "OMN-1")])
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 1


def test_reader_counts_a_newer_supply_row_as_covered():
    demand_raw = _row("OMN-1", "done", 7200)
    supply_raw = _row("OMN-1", "done", 3600)
    observation, _ = _read([(demand_raw,)], [_supply(supply_raw, "OMN-1")])
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 0


def test_reader_counts_each_ticket_of_a_multi_ticket_row():
    raw = _row("OMN-1,OMN-2", "done", 3600)
    observation, _ = _read([(raw,)], [_supply(raw, "OMN-1")])
    assert observation.demand_done_tickets == 2
    assert observation.unprojected_tickets == 1


def test_reader_counts_refused_rows():
    raw = _row("OMN-1", "done", 3600)
    observation, _ = _read([("not a ledger row",), (raw,)], [_supply(raw, "OMN-1")])
    assert observation.refused_rows == 1
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 0


def test_reader_without_done_events_skips_the_supply_read():
    raw = _row("OMN-1", "handed-off", 3600)
    observation, executed = _read([(raw,)], [])
    assert observation.demand_done_tickets == 0
    assert observation.unprojected_tickets == 0
    assert len(executed) == 1


def test_reader_binds_the_window_from_the_check():
    _observation, executed = _read([], [])
    assert executed[0][1] == (
        NOW - timedelta(seconds=1234),
        NOW - timedelta(seconds=5678),
    )
