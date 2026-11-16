# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the scheduled work-state projection reading its ledger through the bus (OMN-23691).

The tick goes through the real handler and the real file_sources profile; the ledger comes through the overlay's command route.
Seam: the request the command row source (node_reading_writer_effect) sends against the work ledger query node's request
model and handler, and the answer that node gives against the row source's reading of it. The planted break renames the key
that carries each row's text in the answer; the read then names the ledger unread, so the seam test fails when either side
changes shape.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.models.work_state_projection import ModelWorkStateScheduleOverlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.ledger_route_support import (
    ROUTE,
    ROUTE_DOCUMENT,
    FakeBusReadCommand,
    environment,
    install_fake,
    ledger_file,
    ledger_line,
    snapshots_text,
    unread_for,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit

PROJECTION_FILE = ".onex_state/work-state/work-state.json"


def routed(tmp_path: Path, env: dict[str, str]) -> HandlerWorkStateProjectionScheduled:
    return HandlerWorkStateProjectionScheduled(
        schedule=schedule(
            projector="file_sources",
            state_home=str(tmp_path),
            ledger_read=ROUTE_DOCUMENT,
        ),
        environ=env,
    )


def test_golden_ledger_route_tick_reads_ledger_through_the_bus_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake(
        monkeypatch,
        FakeBusReadCommand([ledger_line("CLAIM", "lane-a", "ZEP-1")]),
    )
    event = routed(tmp_path, environment(tmp_path)).handle(tick(T0))
    assert event is not None
    assert unread_for(event.unread, "ledger") == []
    assert fake.requests
    assert (tmp_path / PROJECTION_FILE).is_file()
    assert "lane-a" in snapshots_text(tmp_path)


def test_error_ledger_route_failed_read_is_named_in_the_event_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(monkeypatch, FakeBusReadCommand([], exit_code=1))
    event = routed(tmp_path, environment(tmp_path)).handle(tick(T0))
    assert event is not None
    entries = unread_for(event.unread, "ledger")
    assert len(entries) == 1
    assert "read-command-exit-1" in entries[0]


def test_error_ledger_route_unset_command_is_named_never_read_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake(monkeypatch, FakeBusReadCommand([]))
    env = environment(
        tmp_path,
        with_command=False,
        ledger_file=ledger_file(tmp_path, ledger_line("CLAIM", "file-lane", "ZEP-9")),
    )
    event = routed(tmp_path, env).handle(tick(T0))
    assert event is not None
    entries = unread_for(event.unread, "ledger")
    assert len(entries) == 1
    assert "ledger-route-unconfigured" in entries[0]
    assert fake.requests == []
    assert "file-lane" not in snapshots_text(tmp_path)


def test_ledger_route_overlay_document_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ModelWorkStateScheduleOverlay.model_validate(
            {
                "run_interval_seconds": 60,
                "projector": "file_sources",
                "ledger_read": {**ROUTE_DOCUMENT, "path": "ledger-fixture.md"},
            }
        )
    assert schedule().ledger_read is None
    assert schedule(ledger_read=ROUTE_DOCUMENT).ledger_read == ROUTE


class InMemoryLedgerStore:
    """The work ledger query node's store, in memory: the narrowing read the node asks of a real one."""

    def __init__(self, records: tuple[ModelWorkLedgerRowRecord, ...]) -> None:
        self.records = records

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            r
            for r in self.records
            if (since is None or r.row_ts >= since) and r.row_ts <= until
        )

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            r
            for r in self.read_rows(since=since, until=until)
            if (not kinds or r.row_type in kinds)
            and (not contains or contains in r.raw_row)
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return ()

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        shown = [r for r in self.records if r.row_ts <= until]
        return max(r.row_ts for r in shown), max(
            r.projected_at for r in shown if r.projected_at is not None
        )


def record(kind: str, lane: str, minutes_before: int) -> ModelWorkLedgerRowRecord:
    stamp = T0 - timedelta(minutes=minutes_before)
    text = ledger_line(kind, lane, "ZEP-1", minutes_before)
    return ModelWorkLedgerRowRecord(
        row_id=f"{kind}-{lane}-{minutes_before}",
        row_ts=stamp,
        row_type=kind,
        row_lane=lane,
        text=text,
        raw_row=text,
        source="onex-ledger",
        projected_at=stamp + timedelta(seconds=1),
    )


def query_node_as_command(
    monkeypatch: pytest.MonkeyPatch,
    store: InMemoryLedgerStore,
    rename: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run the real query node behind the command row source's command; return the requests it was sent."""
    sent: list[dict[str, Any]] = []
    handler = HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, store), now=lambda: T0
    )

    def command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        wire = json.loads(kwargs["input"])
        sent.append(wire)
        request = ModelWorkLedgerQueryRequest.model_validate(
            {"correlation_id": uuid4(), "now": T0, **wire}
        )
        answer = json.loads(handler.handle(request).model_dump_json())
        if rename is not None:
            answer = rename(answer)
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    monkeypatch.setattr(subprocess, "run", command)
    return sent


def read_claims(
    env: dict[str, str],
) -> tuple[str | None, tuple[ModelLedgerEvidenceRow, ...]]:
    read = build_row_source(ROUTE, env).read(
        ModelLedgerQuery(
            since=T0 - timedelta(hours=72),
            until=T0 + timedelta(days=1),
            row_type="CLAIM",
        )
    )
    return read.error, read.rows


def test_seam_ledger_route_request_meets_the_query_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryLedgerStore(
        (record("CLAIM", "lane-a", 90), record("TERMINAL", "lane-a", 30))
    )
    sent = query_node_as_command(monkeypatch, store)
    error, rows = read_claims(environment(tmp_path))
    assert error is None
    assert [r.lane for r in rows] == ["lane-a"]
    assert [r.row_type for r in rows] == ["CLAIM"]
    assert sent[0]["kinds"] == ["CLAIM"]
    assert sent[0]["limit"] == ROUTE.row_limit


def test_seam_ledger_route_planted_rename_of_the_answer_key_turns_the_read_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryLedgerStore((record("CLAIM", "lane-a", 90),))

    def rename(answer: dict[str, Any]) -> dict[str, Any]:
        for row in answer["rows"]:
            row["raw"] = row.pop("raw_row")
        return answer

    query_node_as_command(monkeypatch, store, rename)
    error, rows = read_claims(environment(tmp_path))
    assert error == "read-answer-malformed-row"
    assert rows == ()
