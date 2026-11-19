# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22811: the redrive projects a refused record into real Postgres.

Reuses the OMN-15909 harness: a disposable schema carrying the live migrated
delegation projection tables, reached with the INTEGRATION_POSTGRES_* settings,
and skipped without a reachable database. The redrive reads a dead letter,
reads its source record by coordinates, resolves the tenant through the
writer's own registry path and writes the delegation row through the real
``DelegationProjectionRunner``.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers._delegation_boundary import (
    DelegationRedriveBoundary,
)
from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers.handler_projection_dlq_redrive import (
    HandlerProjectionDlqRedrive,
)
from omnimarket.nodes.node_projection_dlq_redrive_effect.models import (
    EnumRedriveOutcome,
    ModelProjectionDlqRedriveRequest,
)
from tests.test_golden_chain_projection_dlq_redrive_effect import (
    FakeDeadLetters,
    FakeSource,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)


@pytest.mark.integration
async def test_redrive_writes_the_refused_delegation_row_to_real_postgres() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        correlation_id = str(uuid4())
        topic = runner._topic_delegation_completed
        payload = _real_delegation_completed_payload(
            correlation_id=correlation_id, tenant_id="beta-business-proof"
        )
        wire = json.dumps(
            {"tenant_id": "beta-business-proof", "payload": payload}
        ).encode()
        envelope = json.dumps(
            {
                "correlation_id": correlation_id,
                "failure_reason": "tenant is not registered",
                "source_topic": topic,
                "source_partition": 4,
                "source_offset": 1200,
            }
        ).encode()
        boundary = DelegationRedriveBoundary(runner)
        handler = HandlerProjectionDlqRedrive(
            dead_letters=FakeDeadLetters([envelope]),
            source=FakeSource({(topic, 4, 1200): wire}),
            tenants=boundary,
            projector=boundary,
        )

        result = await handler.handle(
            ModelProjectionDlqRedriveRequest(correlation_ids=[correlation_id])
        )

        assert result.records[0].outcome is EnumRedriveOutcome.REPROJECTED
        row = await admin_conn.fetchrow(
            "SELECT correlation_id FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert row is not None
        count = await admin_conn.fetchval(
            "SELECT count(*) FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert count == 1
