# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20071 AC4: read one ticket's verdict through the projection access node."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
    build_sqlite_window_query,
)
from omnimarket.projection import api_server
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    parse_order_by_clauses,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import build_window_query
from tests.unit.nodes.node_projection_read_effect.test_projection_read_effect_omn20159 import (
    _RecordingSource,
)

TOPIC = "onex.snapshot.projection.dod-verdict.v1"
COLUMNS = (
    "ticket_id",
    "correlation_id",
    "completed_at",
    "started_at",
    "status",
    "unresolved_cause",
    "outcome",
    "outcome_refusal",
    "total_checks",
    "verified_count",
    "failed_count",
    "skipped_count",
    "contract_source",
    "contract_repository",
    "contract_commit_sha",
    "contract_repo_path",
    "projected_at",
    "projection_cursor",
)


def _cfg(**overrides: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": TOPIC,
        "table": "dod_verify_runs",
        "schema_name": "omnidash_analytics",
        "relation_schema": "omninode_internal",
        "columns": COLUMNS,
        "order_by": "completed_at DESC",
        "order_by_spec": parse_order_by_clauses("completed_at DESC", COLUMNS),
        "cursor_column": "projection_cursor",
        "freshness_column": "projected_at",
        "bus_backed": True,
        "key_columns": ("ticket_id", "correlation_id", "completed_at"),
        "key_grain": "immutable",
        "page_selection": "order_by",
        "limit": 100,
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "ticket_id": ticket,
            "correlation_id": f"run-{i}",
            "completed_at": f"2026-10-07T10:0{i}:00+00:00",
            "projection_cursor": i,
            "contract_source": "product_repository",
            "contract_repository": "OmniNode-ai/omnimarket",
            "contract_commit_sha": str(i) * 40,
            "contract_repo_path": f"contracts/{ticket}.yaml",
        }
        for i, ticket in enumerate(("OMN-20071", "OMN-20070", "OMN-20071"), 1)
    ]


class TicketSource(_RecordingSource):
    async def rows(
        self, cfg: ProjectionTableConfig, *, ticket_id: str | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.ticket_id = ticket_id
        return await super().rows(cfg, **kwargs)


def test_real_contract_declares_the_complete_verdict_exposure() -> None:
    cfg = build_projection_topic_map()[TOPIC]
    assert cfg.bus_backed is True
    assert cfg.columns == COLUMNS
    assert cfg.relation_schema == "omninode_internal"
    assert cfg.cursor_column == "projection_cursor"
    assert cfg.freshness_column == "projected_at"
    assert cfg.key_columns == ("ticket_id", "correlation_id", "completed_at")
    assert cfg.key_grain == "immutable"
    assert cfg.page_selection == "order_by"
    assert cfg.limit == 100


async def test_node_returns_only_the_ticket_newest_first_with_contract_subject() -> (
    None
):
    source = TicketSource(_rows())
    handler = HandlerProjectionRead(topic_map={TOPIC: _cfg()}, row_source=source)
    result = await handler.handle(
        ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071")
    )
    assert result.ok, result
    assert result.row_count == 2
    assert [r["correlation_id"] for r in result.rows] == ["run-3", "run-1"]
    assert source.ticket_id == "OMN-20071"
    assert all(r["ticket_id"] == "OMN-20071" for r in result.rows)
    assert result.rows[0]["contract_commit_sha"] == "3" * 40
    assert all(
        all(r[c] for c in COLUMNS if c.startswith("contract_")) for r in result.rows
    )
    latest = await handler.handle(
        ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071", limit=1)
    )
    assert [r["correlation_id"] for r in latest.rows] == ["run-3"]


async def test_ticket_filter_without_declared_column_is_a_named_refusal() -> None:
    source = TicketSource(_rows())
    cfg = _cfg(columns=tuple(c for c in COLUMNS if c != "ticket_id"))
    result = await HandlerProjectionRead(
        topic_map={TOPIC: cfg}, row_source=source
    ).handle(ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071"))
    assert not result.ok
    assert result.http_status == 422
    assert result.error == "unsupported_filter"
    assert result.response["filter"] == "ticket_id"
    assert result.rows == []
    assert source.tenants == []


def test_empty_ticket_is_invalid() -> None:
    with pytest.raises(ValidationError) as exc:
        ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="")
    assert exc.value.errors()[0]["type"] == "string_too_short"


async def test_wildcard_columns_support_ticket_filter() -> None:
    result = await HandlerProjectionRead(
        topic_map={TOPIC: _cfg(columns=("*",))}, row_source=TicketSource(_rows())
    ).handle(ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071"))
    assert result.ok
    assert result.row_count == 2


async def test_filtered_newest_window_is_not_used_for_exposure_freshness() -> None:
    source = TicketSource(_rows())
    source.latest_event_at = AsyncMock(return_value=None)
    cfg = _cfg(cursor_column=None, page_selection="cursor")
    await HandlerProjectionRead(topic_map={TOPIC: cfg}, row_source=source).handle(
        ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071")
    )
    assert source.latest_event_at.call_args.kwargs["window_rows"] is None


async def test_http_ticket_query_returns_the_nodes_page() -> None:
    source = TicketSource(_rows())
    topics = {TOPIC: _cfg()}
    api_server.app.dependency_overrides[api_server.get_topic_map] = lambda: topics
    api_server.app.dependency_overrides[api_server.get_row_source] = lambda: source
    try:
        http = TestClient(api_server.app).get(
            f"/projection/{TOPIC}", params={"ticket_id": "OMN-20071", "limit": 1}
        )
    finally:
        api_server.app.dependency_overrides.clear()
    result = await HandlerProjectionRead(topic_map=topics, row_source=source).handle(
        ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071", limit=1)
    )
    expected, actual = http.json(), dict(result.response)
    for body in (expected, actual):
        body.pop("generated_at", None)
    assert http.status_code == result.http_status == 200
    assert expected == actual
    assert expected["rows"][0]["correlation_id"] == "run-3"


def test_postgres_ticket_predicate_is_bound_alongside_other_filters() -> None:
    cfg = _cfg(tenant_column="tenant_id", columns=(*COLUMNS, "tenant_id"))
    injected = "OMN-20071' OR true --"
    query = build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id="tenant",
        correlation_id="run",
        ticket_id=injected,
        since="5",
        since_type="bigint",
    )
    assert query.params == ("tenant", "run", injected, "5")
    assert '"ticket_id"::text = $3' in query.sql
    assert injected not in query.sql


async def test_sqlite_ticket_predicate_scopes_the_window(tmp_path: Path) -> None:
    cfg = _cfg(limit=1)
    query = build_sqlite_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=None,
        ticket_id="OMN-20071",
        correlation_id="run-3",
    )
    assert query.params == ("run-3", "OMN-20071")
    db_path = tmp_path / "projections.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE dod_verify_runs ("
            + ", ".join(f'"{c}" TEXT' for c in COLUMNS)
            + ")"
        )
        for row in _rows():
            conn.execute(
                "INSERT INTO dod_verify_runs ("
                + ", ".join(row)
                + ") VALUES ("
                + ", ".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
    result = await HandlerProjectionRead(
        topic_map={TOPIC: cfg}, row_source=SqliteTableRowSource(db_path)
    ).handle(
        ModelProjectionReadRequest(
            topic=TOPIC, row_ticket_id="OMN-20071", row_correlation_id="run-3"
        )
    )
    assert result.ok, result
    assert [r["correlation_id"] for r in result.rows] == ["run-3"]


def test_each_verdict_run_owns_its_own_key_and_redelivery_repeats_it() -> None:
    """OMN-18908: all three run coordinates distinguish immutable snapshot keys."""
    from datetime import timedelta

    from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
        HandlerProjectionDodVerdict,
    )
    from omnimarket.nodes.node_projection_dod_verdict.models import (
        ModelDodVerdictProjectionRequest,
        ModelDodVerdictWire,
    )
    from omnimarket.projection.snapshot_publisher import encode_snapshot_delta
    from tests.test_omn20696_dod_verdict_rebuild_real_postgres import _STARTED, _event

    cfg = build_projection_topic_map()[TOPIC]
    original = _event(ticket_id="OMN-20071")
    events = [
        original,
        {**original, "ticket_id": "OMN-20070"},
        {**original, "correlation_id": "20071000-0000-4000-8000-000000000001"},
        {**original, "completed_at": (_STARTED + timedelta(minutes=9)).isoformat()},
        dict(original),
    ]
    keys = []
    for offset, event in enumerate(events):
        row = (
            HandlerProjectionDodVerdict()
            .handle(
                ModelDodVerdictProjectionRequest(
                    event=ModelDodVerdictWire.model_validate(event)
                )
            )
            .row
        )
        assert row is not None
        message = encode_snapshot_delta(
            cfg,
            op="upsert",
            row=row.model_dump(mode="json"),
            source_event_id=event["correlation_id"],
            source_topic="onex.evt.omnimarket.dod-verify-completed.v1",
            source_partition=0,
            source_offset=offset,
            observed_at=_STARTED.isoformat(),
        )
        assert message is not None
        keys.append(message.key)
    assert len(set(keys[:4])) == 4, (
        "changing any run coordinate must give the run its own key"
    )
    assert keys[0] == keys[4], "a redelivery must retain the original run's key"
