# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20071: the real verdict writer's rows read through the access node."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_dod_verdict.handlers import (
    handler_dod_verdict_runner as writer_module,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.table_reader import TableRowSource
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as local_postgres,
)
from tests.test_omn19514_dod_verdict_delegation_run_real_postgres import _ConnectionDb
from tests.test_omn20696_dod_verdict_rebuild_real_postgres import (
    _STARTED,
    MIGRATIONS,
    _event,
)

TOPIC = "onex.snapshot.projection.dod-verdict.v1"
pytestmark = pytest.mark.integration


@pytest.fixture
def dsn(request: pytest.FixtureRequest) -> str:
    """Use CI's database when bound, else the shared disposable local fixture."""
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD")
    if password:
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
        database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    pg = request.getfixturevalue("local_postgres")[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


@pytest.mark.integration
async def test_real_writer_subjects_are_read_only_for_the_requested_ticket(
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = await asyncpg.connect(dsn)
    schema = f"omn20071_{uuid4().hex[:12]}"
    source = TableRowSource(
        environ={"OMNINODE_INTERNAL_DB_URL": dsn, "OMNIDASH_ANALYTICS_DB_URL": dsn}
    )
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        for migration in MIGRATIONS:
            await connection.execute(
                migration.read_text().replace("omninode_internal.", f"{schema}.")
            )
        monkeypatch.setattr(writer_module, "TABLE", f"{schema}.dod_verify_runs")
        monkeypatch.setattr(
            writer_module,
            "_UPSERT",
            writer_module._UPSERT.replace("omninode_internal.", f"{schema}."),
        )
        writer = DodVerdictProjectionWriter()
        monkeypatch.setattr(writer, "_db", _ConnectionDb(connection))
        producer = AsyncMock()
        monkeypatch.setattr(
            writer, "_ensure_producer", AsyncMock(return_value=producer)
        )
        events = [
            _event(
                ticket_id="OMN-20071", contract_repo_path="contracts/OMN-20071.yaml"
            ),
            _event(
                ticket_id="OMN-20071",
                completed_at=(_STARTED + timedelta(minutes=9)).isoformat(),
                contract_source="onex_change_control",
                contract_repository="OmniNode-ai/onex_change_control",
                contract_commit_sha="e" * 40,
                contract_repo_path="contracts/OMN-20071.yaml",
            ),
            _event(
                ticket_id="OMN-20070",
                completed_at=(_STARTED + timedelta(minutes=20)).isoformat(),
            ),
        ]
        for offset, event in enumerate(events):
            await writer.project_event(
                writer.topics[0],
                event,
                MessageMeta(
                    partition=0,
                    offset=offset,
                    fallback_id=event["correlation_id"],
                    topic=writer.topics[0],
                ),
            )
        cfg = build_projection_topic_map()[TOPIC]
        assert cfg.relation_schema == "omninode_internal"
        cfg = cfg.model_copy(update={"relation_schema": schema})
        handler = HandlerProjectionRead(topic_map={TOPIC: cfg}, row_source=source)
        result = await handler.handle(
            ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071")
        )
        assert result.ok, result
        assert result.row_count == 2
        assert {r["ticket_id"] for r in result.rows} == {"OMN-20071"}
        for row, event in zip(result.rows, reversed(events[:2]), strict=True):
            for column in (
                "contract_source",
                "contract_repository",
                "contract_commit_sha",
                "contract_repo_path",
            ):
                assert row[column] == event[column]
        latest = await handler.handle(
            ModelProjectionReadRequest(topic=TOPIC, row_ticket_id="OMN-20071", limit=1)
        )
        assert latest.rows == result.rows[:1]
        assert producer.send_and_wait.await_count == 3
        for offset, (call, event) in enumerate(
            zip(producer.send_and_wait.call_args_list, events, strict=True)
        ):
            assert call.args[0] == TOPIC
            delta = json.loads(call.kwargs["value"])
            assert delta["row"]["ticket_id"] == event["ticket_id"]
            assert delta["row"]["projection_cursor"] is not None
            assert delta["source_topic"] == writer.topics[0]
            assert delta["source_offset"] == offset
            assert delta["source_event_id"] == event["correlation_id"]
            assert set(delta["row"]) == set(cfg.columns)
            for column in (
                "contract_source",
                "contract_repository",
                "contract_commit_sha",
                "contract_repo_path",
            ):
                assert delta["row"][column] == event[column]
    finally:
        await source.close()
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
