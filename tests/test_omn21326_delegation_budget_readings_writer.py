# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_budget_readings effect-class writer and its contract (OMN-21326 AC3, OMN-17427)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer import (
    _SELECT_PRIOR,
    _UPSERT,
    DelegationBudgetReadingsProjectionWriter,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
    ModelDelegationBudgetReadingRow,
)
from tests.test_omn21326_delegation_budget_readings_projection import (
    COMPLETED_TOPIC,
    CORRELATION_ID,
    T0,
    _delivery,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation_budget_readings"
)
_ORCHESTRATOR_CONTRACT = (
    _NODE.parent / "node_delegate_skill_orchestrator" / "contract.yaml"
)


class _Db:
    def __init__(self, prior: list[dict[str, Any]] | None = None) -> None:
        self.prior = prior or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if query == _SELECT_PRIOR:
            return self.prior
        if query == _UPSERT:
            return [
                {
                    "correlation_id": params[0],
                    "status": params[1],
                    "observed_at": params[10],
                    "projection_cursor": 1,
                }
            ]
        raise AssertionError(f"unexpected query: {query}")


def _writer(
    db: _Db, monkeypatch: pytest.MonkeyPatch
) -> DelegationBudgetReadingsProjectionWriter:
    writer = DelegationBudgetReadingsProjectionWriter()
    monkeypatch.setattr(writer, "_db", db)

    async def _no_producer() -> None:
        return None

    monkeypatch.setattr(writer, "_stop_producer", _no_producer)
    return writer


def test_the_producers_terminal_projects_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db()
    out = _writer(db, monkeypatch).handle(_delivery())
    assert out["rows_upserted"] == 1
    assert out["row"]["correlation_id"] == str(CORRELATION_ID)
    (select, select_params), (upsert, params) = db.calls
    assert select == _SELECT_PRIOR
    assert select_params == (CORRELATION_ID,)
    assert upsert == _UPSERT
    assert params == (
        CORRELATION_ID,
        "completed",
        "research",
        COMPLETED_TOPIC,
        50,
        50,
        61_000,
        120,
        "dev",
        "OMN-21326",
        T0,
    )


def test_the_writer_upserts_under_the_observed_at_guard() -> None:
    assert "ON CONFLICT (correlation_id)" in _UPSERT
    assert (
        "WHERE omninode_internal.delegation_budget_readings.observed_at "
        "< EXCLUDED.observed_at" in _UPSERT
    )


def test_a_stored_newer_terminal_means_no_upsert_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = ModelDelegationBudgetReadingRow(
        correlation_id=CORRELATION_ID,
        status="completed",
        task_type="research",
        observed_at=datetime(2999, 1, 1, tzinfo=UTC),
    ).model_dump()
    db = _Db(prior=[stored])
    assert _writer(db, monkeypatch).handle(_delivery()) == {
        "rows_upserted": 0,
        "row": None,
    }
    assert [query for query, _ in db.calls] == [_SELECT_PRIOR]


def test_an_untimed_terminal_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db()
    with pytest.raises(ValueError, match="no envelope timestamp"):
        _writer(db, monkeypatch).handle(_delivery(None))
    assert db.calls == []


def test_only_the_writer_is_routed_and_it_dispatches_in_process() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    assert [h["operation"] for h in handlers] == [
        "delegation_budget_readings_projection_writer"
    ]
    assert DelegationBudgetReadingsProjectionWriter.onex_runtime_inprocess_dispatch


def test_the_node_subscribes_to_exactly_the_terminals_the_lane_publishes() -> None:
    projection = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    orchestrator = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text(encoding="utf-8"))
    published = set(orchestrator["event_bus"]["publish_topics"])
    subscribed = projection["event_bus"]["subscribe_topics"]
    assert sorted(subscribed) == [
        "onex.evt.omnimarket.delegate-skill-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
    ]
    assert set(subscribed) <= published


def test_the_readback_reads_the_table_this_node_writes() -> None:
    projection = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    orchestrator = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text(encoding="utf-8"))
    table = projection["db_io"]["db_tables"][0]
    source = orchestrator["budget_readback"]["source"]
    assert source["relation"] == f"{table['schema']}.{table['name']}"
    assert source["populated_by"] == "omnimarket.nodes." + _NODE.name


def test_no_host_or_credential_is_written_in_the_readback_contract() -> None:
    text = _ORCHESTRATOR_CONTRACT.read_text(encoding="utf-8")
    block = text[text.index("\nbudget_readback:") :]
    assert "postgres://" not in block
    assert "postgresql://" not in block
