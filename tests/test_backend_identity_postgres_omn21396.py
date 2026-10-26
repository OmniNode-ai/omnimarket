# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serving identity survives the real writer's JSONB upsert and replay.

Uses an owned PostgreSQL container, never an INTEGRATION_POSTGRES live database.
"""

from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)
from tests.test_omn18147_delegation_completed_using_rls import (
    local_postgres as local_postgres,
)


@pytest.mark.integration
async def test_canonical_identity_survives_real_jsonb_write_and_replay(local_postgres):
    dsn = f"postgresql://postgres@{local_postgres.host}:{local_postgres.port}/{local_postgres.database}"
    async with _provisioned_runner(dsn) as (runner, connection, _schema):
        cid = str(uuid4())
        identity = {
            "host": "inference.example",
            "model_file": "weights.gguf",
            "quant": "Q8_0",
            "context_tokens": 65536,
        }
        data = _real_delegation_completed_payload(correlation_id=cid)
        data["escalation_history"] = [
            {
                "tier_name": "local",
                "backend_ref": "local-a",
                "model_used": "shared-model",
                "host": identity["host"],
                "acceptance_decision": "accept",
                "configuration": {"serving_identity": identity},
            }
        ]
        meta = MessageMeta(partition=0, offset=0, fallback_id=cid)
        assert await runner.project_event(
            runner._topic_delegation_completed, data, meta
        )
        import json

        attribution = await connection.fetchrow(
            "SELECT backend_id, host FROM delegation_events WHERE correlation_id=$1",
            cid,
        )
        assert attribution["backend_id"] == "local-a"
        assert attribution["host"] == identity["host"]
        stored = await connection.fetchval(
            "SELECT attempt_history FROM delegation_events WHERE correlation_id=$1", cid
        )
        history = json.loads(stored) if isinstance(stored, str) else stored
        assert history[0]["identity_status"] == "recorded"
        assert history[0]["configuration"]["serving_identity"] == identity
        assert await runner.project_event(
            runner._topic_delegation_completed, data, meta
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM delegation_events WHERE correlation_id=$1", cid
            )
            == 1
        )
