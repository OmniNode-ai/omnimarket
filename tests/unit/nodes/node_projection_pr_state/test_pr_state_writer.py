# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule 7a routing and the SQL boundary."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method

from omnimarket.nodes.node_projection_pr_state.handlers.handler_pr_state_projection import (
    PrStateProjectionWriter,
)
from omnimarket.nodes.node_projection_pr_state.handlers.pr_state_fold import (
    HandlerProjectionPrState,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = pytest.mark.unit


class FakeDb:
    def __init__(self, fail: bool = False) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.connected = self.closed = 0
        self.fail = fail

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1

    async def execute(self, sql: str, *args: object) -> None:
        if self.fail:
            raise RuntimeError("write failed")
        self.statements.append((sql, args))


def writer(db: FakeDb) -> PrStateProjectionWriter:
    w = PrStateProjectionWriter()
    w._db = db  # type: ignore[assignment]
    return w


def test_writer_routing_and_contract() -> None:
    w = writer(FakeDb())
    assert w.onex_runtime_inprocess_dispatch is True
    assert w.subscribe_topics == ["onex.evt.omnimarket.pr-state-observed.v1"]
    path = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_projection_pr_state/contract.yaml"
    )
    c = yaml.safe_load(path.read_text())
    assert set(c["runtime_lanes"]) == {"compose-dev", "onex-lab", "onex-lab-k3s"}
    # OMN-19833: only the writer is routed; it calls the pure fold in process.
    assert {h["handler"]["name"] for h in c["handler_routing"]["handlers"]} == {
        "PrStateProjectionWriter",
    }
    assert c["event_bus"]["subscribe_topics"] == w.subscribe_topics
    assert len(c["event_bus"]["dlq_topics"]) == 1


def test_runtime_calls_pure_fold() -> None:
    result = _invoke_handle_method(
        HandlerProjectionPrState().handle, event().model_dump(mode="json")
    )
    assert result.event == event()


def test_writer_binds_timestamptz_and_guards_in_sql() -> None:
    db = FakeDb()
    w = writer(db)
    payload = event().model_dump(mode="json") | {"_topic": w.subscribe_topics[0]}
    original = dict(payload)
    assert w.handle(payload) == {"rows_upserted": 1}
    assert payload == original
    assert (db.connected, db.closed) == (1, 1)
    sql, args = db.statements[0]
    assert "ON CONFLICT (repo, pr_number) DO UPDATE" in sql
    assert "< (EXCLUDED.observed_at, EXCLUDED.last_digest)" in sql
    assert args[:2] == (event().repo, event().pr_number)
    assert any(isinstance(a, datetime) and a.tzinfo is not None for a in args)
    assert "DELETE" not in sql


@pytest.mark.asyncio
async def test_sync_handle_works_inside_running_loop() -> None:
    assert writer(FakeDb()).handle(event().model_dump(mode="json")) == {
        "rows_upserted": 1
    }


def test_failure_closes_database_and_propagates() -> None:
    db = FakeDb(True)
    with pytest.raises(RuntimeError, match="write failed"):
        writer(db).handle(event().model_dump(mode="json"))
    assert db.closed == 1


def test_wrong_topic_writes_nothing() -> None:
    db = FakeDb()
    with pytest.raises(ValueError, match="unsubscribed"):
        writer(db).handle(event().model_dump(mode="json") | {"_topic": "wrong"})
    assert db.statements == []
