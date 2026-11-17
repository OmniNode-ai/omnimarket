# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_work_ledger_graph_query_effect.

A command payload as it arrives on the contract's command topic is validated into
the contract's input model, answered over projected rows behind a fake reader, and
the answer round-trips through the contract's output model. The error leg is an
unreadable store: an error answer, never an empty one.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.work_ledger_graph import (
    EnumWorkLedgerGraphQuery,
    ModelWorkLedgerGraphRow,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect import (
    NodeWorkLedgerGraphQueryEffect,
)

pytestmark = pytest.mark.unit

CONTRACT = yaml.safe_load(
    (
        Path(__file__).parent.parent
        / "src/omnimarket/nodes/node_work_ledger_graph_query_effect/contract.yaml"
    ).read_text(encoding="utf-8")
)
NOW = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)


def _model(dotted: str) -> type:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)  # type: ignore[no-any-return]


def _row(ts: str, kind: str, cells: str) -> ModelWorkLedgerGraphRow:
    return ModelWorkLedgerGraphRow(
        row_id=f"{ts}-{kind}",
        row_ts=datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
        row_type=kind,
        raw_row=f"{ts} | {kind} | {cells} | text",
    )


ROWS = [
    _row("2026-11-17T10:00:00Z", "CLAIM", "lane=b | ticket=OMN-2"),
    _row("2026-11-17T10:01:00Z", "STATUS", "lane=b | ticket=OMN-2 | pr=omnimarket#2"),
    _row(
        "2026-11-17T10:02:00Z",
        "TERMINAL",
        "lane=b | ticket=OMN-2 | closes-CLAIM=2026-11-17T10:00:00Z",
    ),
]


class _Reader:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def read_rows(
        self, since: datetime, until: datetime
    ) -> list[ModelWorkLedgerGraphRow]:
        if self.fail:
            raise ConnectionError("store unreachable")
        return [r for r in ROWS if since <= r.row_ts < until]


def test_golden_chain_command_to_terminal_answer() -> None:
    command = {"query": "open_prs_without_owner", "limit": 10}
    request = _model(CONTRACT["input_model"]).model_validate(command)
    result = NodeWorkLedgerGraphQueryEffect(reader=_Reader(), now=lambda: NOW).handle(
        request
    )
    output = _model(CONTRACT["output_model"])
    replayed = output.model_validate_json(result.model_dump_json())
    assert replayed.status == "ok"
    assert replayed.query is EnumWorkLedgerGraphQuery.OPEN_PRS_WITHOUT_OWNER
    assert [i.subject for i in replayed.items] == ["omnimarket#2"]
    assert (
        CONTRACT["runtime_dispatch"]["terminal_events"]["success"]
        in CONTRACT["event_bus"]["publish_topics"]
    )
    assert (
        CONTRACT["runtime_dispatch"]["command_topic"]
        in CONTRACT["event_bus"]["subscribe_topics"]
    )


def test_golden_chain_error_leg_unreadable_store() -> None:
    request = _model(CONTRACT["input_model"]).model_validate(
        {"query": "heads_reviewed_more_than_once"}
    )
    result = NodeWorkLedgerGraphQueryEffect(
        reader=_Reader(fail=True), now=lambda: NOW
    ).handle(request)
    assert result.status == "error"
    assert result.error is not None
    assert result.items == ()
