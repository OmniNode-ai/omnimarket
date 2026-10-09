# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the database work ledger read node (OMN-20738 AC1).

The fixture ledger is loaded the way the projection stores it (one record per
row, keyed by its content hash) behind a fake reader, and every query must
return the same rows the markdown ledger's read verbs returned over the same
file. ``expected_markdown_reads.json`` was recorded by running those verbs
(``query``, ``holds``, ``inbox``, ``open-claims``) over the fixture; each case
names the verb call it recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_text import ledger_row_stamp, split_ledger_rows
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerParityStatus,
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "work_ledger_query"
LEDGER = FIXTURES / "ROLLING_WORK_LEDGER.md"
EXPECTED = json.loads((FIXTURES / "expected_markdown_reads.json").read_text())
CONTRACT = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
NOW = datetime(2026, 10, 2, tzinfo=UTC)
PROJECTED = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)


def _lane(raw: str) -> str | None:
    for cell in raw.split("\n", 1)[0].split(" | ")[2:]:
        if cell.startswith("lane="):
            return cell[len("lane=") :].split()[0]
    return None


def fixture_records() -> tuple[ModelWorkLedgerRowRecord, ...]:
    """The fixture as the projection stores it."""
    records = []
    for raw in split_ledger_rows(LEDGER.read_text()):
        records.append(
            ModelWorkLedgerRowRecord(
                row_id=work_ledger_row_id(raw),
                row_ts=ledger_row_stamp(raw),
                row_type=raw.split(" | ")[1].strip(),
                row_lane=_lane(raw),
                text=raw.split("\n", 1)[0],
                raw_row=raw,
                source="onex-ledger",
                projected_at=PROJECTED,
            )
        )
    return tuple(records)


class FakeReader:
    """Serves the fixture records in the order the database read returns."""

    def __init__(
        self,
        records: tuple[ModelWorkLedgerRowRecord, ...],
        error: Exception | None = None,
    ) -> None:
        self.records = records
        self.error = error
        self.calls: list[tuple[str, object, object]] = []

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self._check()
        self.calls.append(("rows", since, until))
        return tuple(
            r
            for r in self.records
            if (since is None or r.row_ts >= since) and r.row_ts <= until
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self._check()
        self.calls.append(("receipts", since, until))
        return tuple(
            r
            for r in self.records
            if r.row_type == "STATUS"
            and r.row_lane == "work-ledger-parity"
            and since <= r.row_ts <= until
        )

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        self._check()
        shown = [r for r in self.records if r.row_ts <= until]
        if not shown:
            return None, None
        return max(r.row_ts for r in shown), max(
            r.projected_at for r in shown if r.projected_at is not None
        )


def node(reader: FakeReader | None = None) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(
        reader=reader or FakeReader(fixture_records()), now=lambda: NOW
    )


def _stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def request_for(case: dict[str, Any]) -> ModelWorkLedgerQueryRequest:
    """The node request equivalent to the recorded verb call."""
    verb, *flags = case["verb"]
    positional = flags[:1] if verb == "inbox" else []
    flags = flags[len(positional) :]
    pairs = dict(zip(flags[::2], flags[1::2], strict=True))
    fields: dict[str, object] = {"correlation_id": uuid4(), "now": NOW}
    if verb == "query":
        fields["query"] = EnumWorkLedgerQueryKind.ROWS
        renames = {"--ticket": "ticket", "--lane": "lane", "--pr": "pr"}
        renames |= {"--grep": "term", "--id": "row_ref"}
        for flag, value in pairs.items():
            if flag == "--kind":
                fields["kinds"] = tuple(value.split(","))
            elif flag == "--since":
                fields["since"] = _stamp(value)
            else:
                fields[renames[flag]] = value
    elif verb == "holds":
        fields["query"] = EnumWorkLedgerQueryKind.HOLDS
        for flag, value in pairs.items():
            fields[flag.removeprefix("--")] = value
    elif verb == "inbox":
        fields["query"] = EnumWorkLedgerQueryKind.INBOX
        fields["lanes"] = tuple(positional[0].split(","))
    else:
        fields["query"] = EnumWorkLedgerQueryKind.OPEN_CLAIMS
        for flag, value in pairs.items():
            fields["lane" if flag == "--lane" else "claims_since"] = value
    return ModelWorkLedgerQueryRequest.model_validate(fields)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_work_ledger_query_golden_chain(name: str) -> None:
    case = EXPECTED[name]
    request = request_for(case)
    result = node().handle(request)
    assert result.error is None
    assert result.correlation_id == request.correlation_id
    assert (
        ModelWorkLedgerQueryResult.model_validate_json(result.model_dump_json())
        == result
    )
    if request.query is EnumWorkLedgerQueryKind.ROWS:
        assert [r.text for r in result.rows] == case["rows"]
        assert result.matched == case["matched"]
    elif request.query is EnumWorkLedgerQueryKind.HOLDS:
        assert [h.row.text for h in result.holds] == case["rows"]
        if case["counts"]:
            assert result.hold_counts is not None
            assert result.hold_counts.model_dump() == case["counts"]
    elif request.query is EnumWorkLedgerQueryKind.INBOX:
        (inbox,) = result.inbox
        assert [e.row.text for e in inbox.entries] == case["opened"]
        assert [e.row.text for e in inbox.entries if e.needs_action] == case["action"]
    else:
        got = [
            {
                "lane": c.lane,
                "claim": c.claim.text,
                "ticket": c.ticket,
                "newest_row_stamp": c.newest_row_stamp,
                "newest_row_kind": c.newest_row_kind,
                "rows": c.rows,
                "host": c.host,
                "run": c.run,
                "leases": list(c.leases),
            }
            for c in result.open_claims
        ]
        assert got == case["claims"]


def test_work_ledger_query_golden_chain_rulings_kind_needs_no_filter() -> None:
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.RULINGS, now=NOW
    )
    result = node().handle(request)
    assert [r.text for r in result.rows] == EXPECTED["rulings"]["rows"]


def test_work_ledger_query_golden_chain_newest_per_lane() -> None:
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(),
        query=EnumWorkLedgerQueryKind.NEWEST_PER_LANE,
        now=NOW,
    )
    result = node().handle(request)
    newest = {r.row_lane: r.text[:20] for r in result.rows}
    assert newest == {
        "lane-a": "2026-10-01T01:05:00Z",
        "lane-b": "2026-10-01T01:35:00Z",
        "lane-c": "2026-10-01T01:20:00Z",
        "lane-e": "2026-10-01T01:40:00Z",
        "lane-f": "2026-10-01T01:45:00Z",
        "lane-o": "2026-10-01T00:41:00Z",
        "work-ledger-parity": "2026-10-01T01:25:00Z",
    }


def test_work_ledger_query_golden_chain_freshness_and_parity() -> None:
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(),
        query=EnumWorkLedgerQueryKind.ROWS,
        since=_stamp("2026-09-30T00:00:00Z"),
        until=_stamp("2026-09-30T23:59:59Z"),
        now=NOW,
    )
    result = node().handle(request)
    assert result.rows == ()
    assert result.freshness is not None
    assert result.freshness.newest_row_ts is None
    assert result.parity is not None
    assert result.parity.status is EnumWorkLedgerParityStatus.EXACT
    (day,) = result.parity.days
    assert (day.day, day.exact, day.missing, day.extra) == ("2026-09-30", True, 0, 0)

    wider = node().handle(
        request.model_copy(update={"until": _stamp("2026-10-01T23:59:59Z")})
    )
    assert wider.freshness is not None
    assert wider.freshness.newest_row_ts == _stamp("2026-10-01T01:45:00Z")
    assert wider.freshness.newest_projected_at == PROJECTED
    assert wider.parity is not None
    assert wider.parity.status is EnumWorkLedgerParityStatus.UNMEASURED
    assert [(d.day, d.exact) for d in wider.parity.days] == [
        ("2026-09-30", True),
        ("2026-10-01", None),
    ]


def test_work_ledger_query_golden_chain_not_exact_receipt_wins() -> None:
    records = fixture_records()
    receipt = next(r for r in records if r.row_lane == "work-ledger-parity")
    raw = receipt.raw_row.replace("missing=0", "missing=3").replace(
        "exact=yes", "exact=no"
    )
    newer = receipt.model_copy(
        update={
            "raw_row": raw,
            "text": raw,
            "row_id": work_ledger_row_id(raw),
            "row_ts": _stamp("2026-10-01T01:26:00Z"),
        }
    )
    result = node(FakeReader((*records, newer))).handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            since=_stamp("2026-09-30T00:00:00Z"),
            until=_stamp("2026-09-30T23:59:59Z"),
            now=NOW,
        )
    )
    assert result.parity is not None
    assert result.parity.status is EnumWorkLedgerParityStatus.NOT_EXACT
    assert result.parity.days[0].missing == 3


def test_work_ledger_query_golden_chain_database_error_is_not_an_empty_answer() -> None:
    reader = FakeReader(fixture_records(), RuntimeError("database offline"))
    result = node(reader).handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.HOLDS, now=NOW
        )
    )
    assert result.error == "RuntimeError: database offline"
    assert result.holds == ()
    assert result.hold_counts is None
    assert result.freshness is None


def test_work_ledger_query_golden_chain_continuation_lines_kept_in_raw_row() -> None:
    result = node().handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            kinds=("FRICTION",),
            now=NOW,
        )
    )
    (row,) = result.rows
    assert "\n" not in row.text
    assert row.raw_row.endswith("a continuation line of the friction row")


def test_work_ledger_query_golden_chain_contract_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    handler = node()
    assert handler.terminal_event == contract["terminal_event"]
    assert (
        handler.terminal_event == "onex.evt.omnimarket.work-ledger-query-completed.v1"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in (contract["event_bus"]["subscribe_topics"])
    )
    assert handler.terminal_event in contract["event_bus"]["publish_topics"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["event_model"].endswith("ModelWorkLedgerQueryRequest")
    assert contract["output_model"].endswith("ModelWorkLedgerQueryResult")


def test_work_ledger_query_golden_chain_real_runtime_typed_dispatch() -> None:
    import asyncio

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    handler = node()
    callback = _make_dispatch_callback(handler, event_model=route.event_model)
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.OPEN_CLAIMS, now=NOW
    )
    dispatch = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": str(request.correlation_id),
            }
        )
    )
    assert dispatch is not None
    terminal = ModelWorkLedgerQueryResult.model_validate(dispatch.output_events[0])
    assert [c.lane for c in terminal.open_claims] == ["lane-b", "lane-a", "lane-f"]
