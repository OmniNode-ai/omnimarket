# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: both writers store the pair an unpaired terminal's evidence decides.

A harness delegate-skill terminal and a handler-budget timeout declare no
``operational_outcome`` or ``content_verdict``. Written through the deployed
async runner and the sync handler onto the migrated ``delegation_events`` table,
the success stores completed/usable and the timeout stores
timeout/not_applicable. The shared postgres fixture uses CI's
INTEGRATION_POSTGRES_* settings or owns a disposable PostgreSQL 16 container
when no database is configured. Run with --refuse-skips for acceptance proof.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.test_omn19448_terminal_acceptance_real_postgres import _MIGRATIONS
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    _MIGRATIONS_DIR,
    _NullPublisher,
    _Postgres,
    _provisioned,
    _runner,
    postgres,
)

__all__ = ["postgres"]

_TENANT = "00000000-0000-4000-8000-000000019448"
_HARNESS_MODEL = "glm-5.3-flash"


def _harness_success(cid: str) -> dict[str, object]:
    return {
        "correlation_id": cid,
        "tenant_id": _TENANT,
        "task_type": "document",
        "timestamp": datetime.now(UTC).isoformat(),
        "status": "completed",
        "provider": "harness:claude-glm",
        "model_name": _HARNESS_MODEL,
        "delegated_to": _HARNESS_MODEL,
        "response": "harness answer",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "attempts": [
            {
                "tier": "harness",
                "backend_id": f"harness:claude-glm:{_HARNESS_MODEL}",
                "model_id": _HARNESS_MODEL,
                "quality_gate_passed": True,
                "acceptance_decision": "accept",
                "acceptance_reason": "quality_bar_met",
                "failure_class": None,
                "error_message": "",
            }
        ],
    }


def _handler_timeout(cid: str) -> dict[str, object]:
    return {
        "correlation_id": cid,
        "tenant_id": _TENANT,
        "task_type": "summarization",
        "timestamp": datetime.now(UTC).isoformat(),
        "status": "timeout",
        "model_name": "",
        "response": "",
        "quality_gate_passed": False,
        "error_message": "delegation exceeded the handler execution budget",
        "terminal_failure_cause": "timeout",
        "attempts": [],
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("harness-success", (True, None, "completed", "usable")),
        ("handler-timeout", (False, "timeout", "timeout", "not_applicable")),
    ],
)
@pytest.mark.parametrize("writer", ["sync", "async"])
async def test_unpaired_terminal_row_stores_the_pair_its_evidence_decides(
    postgres: _Postgres,
    writer: str,
    case: str,
    expected: tuple[bool, str | None, str, str],
) -> None:
    cid = str(uuid4())
    failed = case == "handler-timeout"
    payload = _handler_timeout(cid) if failed else _harness_success(cid)
    assert "operational_outcome" not in payload
    assert "content_verdict" not in payload

    async with _provisioned(postgres) as (connection, schema):
        await connection.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await connection.execute(
            "INSERT INTO tenant_registry_mirror VALUES ('terminal-proof', $1)",
            UUID(_TENANT),
        )
        for name in _MIGRATIONS:
            await connection.execute((_MIGRATIONS_DIR / name).read_text())
        if writer == "async":
            async with _runner(postgres, schema) as runner:
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
            payload["_event_type"] = (
                "delegate-skill-failed" if failed else "delegate-skill-completed"
            )
            await asyncio.to_thread(
                HandlerProjectionDelegation(publisher=_NullPublisher()).handle,
                {**payload, "_db": adapter},
            )
        row = await connection.fetchrow(
            "SELECT terminal_ok, terminal_failure_cause, operational_outcome, "
            "content_verdict FROM delegation_events WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert tuple(row.values()) == expected
