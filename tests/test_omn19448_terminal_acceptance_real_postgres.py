# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the shipped migrations and read all terminal columns together.

Both terminal families and both database writers must satisfy the same row
contract. The shared postgres fixture uses CI's INTEGRATION_POSTGRES_* settings
or owns a disposable PostgreSQL 16 container when no database is configured.
Run with --refuse-skips for acceptance proof.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    _MIGRATIONS_DIR,
    _NullPublisher,
    _Postgres,
    _provisioned,
    _runner,
    postgres,
)

__all__ = ["postgres"]

_MIGRATIONS = (
    "0052_delegation_events_trace_and_routing.sql",
    "0053_delegation_events_finish_reason_truncated.sql",
    "0058_delegation_events_requested_model_and_timing.sql",
)
_CAUSE_READBACK = (
    Path(__file__).resolve().parents[1]
    / "scripts/ci/delegation_terminal_cause_readback.sql"
)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("family", "failed", "cause"),
    [
        ("canonical", False, None),
        ("canonical", True, "quality_gate_refused"),
        ("canonical", True, None),
        ("delegate-skill", False, None),
        ("delegate-skill", True, "quality_gate_refused"),
    ],
)
@pytest.mark.parametrize("writer", ["sync", "async"])
async def test_migrated_terminal_row_carries_every_column(
    postgres: _Postgres, family: str, writer: str, failed: bool, cause: str | None
) -> None:
    cid, trace = str(uuid4()), str(uuid4())
    expected: dict[str, object] = {
        "trace_id": trace,
        "requested_model": "first-requested-model",
        "routed_model": "answering-model",
        "answering_backend": "answering-backend",
        "finish_reason": "stop",
        "truncated": False,
        "queue_wait_ms": 0,
        "execution_ms": 123,
        "terminal_failure_cause": cause,
        "operational_outcome": "quality_rejected" if failed else "completed",
        "content_verdict": "unusable" if failed else "usable",
    }
    payload: dict[str, object] = {
        "correlation_id": cid,
        "tenant_id": "00000000-0000-4000-8000-000000019448",
        "task_type": "test",
        "timestamp": datetime.now(UTC).isoformat(),
        "status": "failed" if failed else "completed",
        "trace_id": trace,
        "quality_passed": not failed,
        "quality_gate_passed": not failed,
        "model_used": "answering-model",
        "model_name": "answering-model",
        "route": "answering-backend",
        "requested_model": "first-requested-model",
        "queue_wait_ms": 0,
        "execution_duration_ms": 123,
        "finish_reason": "stop",
        "truncated": False,
        "terminal_failure_cause": expected["terminal_failure_cause"],
        "operational_outcome": expected["operational_outcome"],
        "content_verdict": expected["content_verdict"],
        "escalation_history": [{"finish_reason": "stop", "truncated": False}],
        "attempts": [
            {
                "tier": "local",
                "model_id": "first-requested-model",
                "backend_id": "first-backend",
                "quality_gate_passed": False,
                "failure_class": "model_unavailable",
            },
            {
                "tier": "local",
                "model_id": "answering-model",
                "backend_id": "answering-backend",
                "quality_gate_passed": not failed,
                "finish_reason": "stop",
                "truncated": False,
            },
        ],
    }
    if failed and family == "delegate-skill":
        # A refused ladder has no accepting backend; never invent one.
        expected.update(routed_model=None, answering_backend=None)

    async with _provisioned(postgres) as (connection, schema):
        await connection.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await connection.execute(
            "INSERT INTO tenant_registry_mirror VALUES ('terminal-proof', $1)",
            UUID(str(payload["tenant_id"])),
        )
        # Reapply the actual migrations, not a hand-written test schema.
        for name in _MIGRATIONS:
            await connection.execute((_MIGRATIONS_DIR / name).read_text())
        if writer == "async":
            async with _runner(postgres, schema) as runner:
                if family == "canonical":
                    topic = (
                        runner._topic_delegation_failed
                        if failed
                        else runner._topic_delegation_completed
                    )
                else:
                    topic = (
                        runner._topic_delegate_skill_failed
                        if failed
                        else runner._topic_delegate_skill_completed
                    )
                assert await runner.project_event(
                    topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
                )
        else:
            adapter = PostgresSyncProjectionAdapter(postgres.dsn(schema))
            if family == "canonical":
                payload["_event_type"] = (
                    "delegation-failed" if failed else "delegation-completed"
                )
            else:
                payload["_event_type"] = (
                    "delegate-skill-failed" if failed else "delegate-skill-completed"
                )
            await asyncio.to_thread(
                HandlerProjectionDelegation(publisher=_NullPublisher()).handle,
                {**payload, "_db": adapter},
            )
        columns = ", ".join(expected)
        row = await connection.fetchrow(
            f"SELECT {columns}, terminal_ok FROM delegation_events "
            "WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["terminal_ok"] is (not failed)
        assert {key: row[key] for key in expected} == expected

        probe = _CAUSE_READBACK.read_text().replace(":'deploy_boundary'", "$1")
        boundary = datetime(2026, 1, 1, tzinfo=UTC)
        if failed and cause is not None:
            stamp = await connection.fetchval(
                "SELECT written_at FROM delegation_events WHERE correlation_id = $1",
                cid,
            )
            assert stamp is not None
            assert stamp >= boundary
            result = await connection.fetchrow(probe, boundary)
            assert result is not None
            assert result["failed_rows"] == 1
            assert result["failed_null_cause"] == 0
            assert result["acceptance"] == 1
            await connection.execute(
                "UPDATE delegation_events SET terminal_failure_cause = NULL "
                "WHERE correlation_id = $1",
                cid,
            )
        # Both a failed row with NULL cause and an empty failed-row window
        # must make the exact readback query fail, rather than attest success.
        with pytest.raises(asyncpg.DivisionByZeroError):
            await connection.fetchrow(probe, boundary)
        with pytest.raises(asyncpg.DivisionByZeroError):
            await connection.fetchrow(probe, datetime(2100, 1, 1, tzinfo=UTC))
