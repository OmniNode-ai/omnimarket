# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A parity receipt requires row text through both portable emit bindings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.handlers.inmemory_bus_emitter import (
    InMemoryBusEmitter,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    main,
    receipt_exact,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_parity_text_relations import (
    CORRECTION,
    ROWS,
    RULING,
    SINCE,
    UNTIL,
    _project,
    _rid,
)

pytestmark = pytest.mark.unit


class _RecordingAdapter:
    """Injected publish port: retain the actual spool envelope for the parity probe."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def publish(
        self,
        topic: str,
        payload: object,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        assert isinstance(payload, dict)
        self.received.append(json.loads(json.dumps(payload)))


@pytest.mark.parametrize("raw_rows", [None, {}])
def test_absent_text_never_reports_exact_parity(
    raw_rows: dict[str, str] | None,
) -> None:
    log, state, _ = _project(ROWS)
    report = compare(
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        projection_raw_rows=raw_rows,
    )
    assert not report.exact
    assert not receipt_exact(report)
    assert {
        m.key for m in report.mismatches if m.kind == "row_text_missing_in_projection"
    } == set(log)


@pytest.mark.parametrize("binding", ["inmemory", "spool"])
@pytest.mark.parametrize(
    "mutation",
    ["faithful", "missing-all", "missing-one", "changed-words", "dropped-link"],
)
async def test_text_evidence_has_same_verdict_on_both_bindings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: str,
    mutation: str,
) -> None:
    bus = EventBusInmemory()
    await bus.start()
    adapter = _RecordingAdapter()
    emitter = (
        InMemoryBusEmitter(bus)
        if binding == "inmemory"
        else HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    handler = HandlerWorkLedgerEmit(emitter=emitter)
    try:
        for row in ROWS:
            result = await asyncio.to_thread(
                handler.handle, ModelWorkLedgerEmitRequest(row=row)
            )
            assert result.accepted
            assert result.published
        payloads: list[dict[str, Any]]
        if binding == "inmemory":
            payloads = [
                json.loads(event.value) for event in await bus.get_event_history()
            ]
        else:
            payloads = adapter.received
        received = [p.get("payload", p)["raw_row"] for p in payloads]
        assert received == ROWS
        log, state, raw_rows = _project(received)
        if mutation == "missing-all":
            raw_rows.clear()
        elif mutation == "missing-one":
            del raw_rows[_rid(RULING)]
        elif mutation == "changed-words":
            raw_rows[_rid(RULING)] = RULING.replace("use the lab", "use the cloud")
        elif mutation == "dropped-link":
            raw_rows[_rid(CORRECTION)] = CORRECTION.replace(
                " corrects=2026-10-01T09:00:00Z |", ""
            )
        report = compare(
            file_rows=ROWS,
            projection_rows=log,
            projection_state=state,
            since=SINCE,
            until=UNTIL,
            projection_raw_rows=raw_rows,
        )
        assert report.exact is (mutation == "faithful")
        ledger, export = tmp_path / "ledger.md", tmp_path / "projection.json"
        ledger.write_text("\n".join(ROWS) + "\n")
        export.write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "row_id": k,
                            "row_ts": v,
                            **({"raw_row": raw_rows[k]} if k in raw_rows else {}),
                        }
                        for k, v in log.items()
                    ],
                    "state": [{"entity_key": k, **v} for k, v in state.items()],
                },
                default=str,
            )
        )
        args = [
            "--ledger",
            str(ledger),
            "--projection-json",
            str(export),
            "--utc-day",
            "2026-10-01",
        ]
        assert main(args) == (0 if mutation == "faithful" else 1)
        cli_report = json.loads(capsys.readouterr().out)
        assert cli_report["mismatches"] == [
            m.model_dump(mode="json") for m in report.mismatches
        ]
        assert main([*args, "--receipt", "--state-dir", str(tmp_path)]) == (
            0 if mutation == "faithful" else 1
        )
        receipt = capsys.readouterr().out
        assert f"exact={'yes' if mutation == 'faithful' else 'no'}" in receipt
    finally:
        await bus.close()


@pytest.mark.parametrize("field", ["rows_text_compared", "relations_compared"])
def test_incomplete_evidence_cannot_be_certified_by_an_empty_mismatch_list(
    field: str,
) -> None:
    log, state, raw_rows = _project(ROWS)
    report = compare(
        file_rows=ROWS,
        projection_rows=log,
        projection_state=state,
        since=SINCE,
        until=UNTIL,
        projection_raw_rows=raw_rows,
    )
    assert report.exact
    incomplete = report.model_copy(update={field: 0})
    assert incomplete.mismatches == ()
    assert not incomplete.exact
    assert not receipt_exact(incomplete)
