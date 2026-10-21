# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC109: delegate-skill routing columns persist on real Postgres.

Real-Postgres twin of the unit routing-column tests using the live migration set
and DelegationProjectionRunner.project_event(). The module skips without a
configured, reachable INTEGRATION_POSTGRES_* server.
"""

from __future__ import annotations

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _TENANT,
)
from tests.unit.projection.test_delegation_backend_id_and_host_omn20162 import (
    _skill_terminal,
)

_SELECT = (
    "SELECT routed_model, answering_backend FROM delegation_events "
    "WHERE correlation_id = $1"
)


@pytest.mark.integration
async def test_skill_terminal_stores_and_preserves_routing_columns() -> None:
    payload = _skill_terminal(
        [
            {
                "tier": "cheap_cloud",
                "backend_id": "openrouter-x",
                "model_id": "m-refused",
                "quality_gate_passed": False,
                "failure_class": "model_unavailable",
                "host": None,
            },
            {
                "tier": "local",
                "backend_id": "local-coder",
                "model_id": "Qwen3.8-27B",
                "quality_gate_passed": True,
                "failure_class": None,
                "host": "h201",
            },
        ]
    )
    payload["tenant_id"] = _TENANT
    cid = str(payload["correlation_id"])
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        topic = runner._topic_delegate_skill_completed
        assert await runner.project_event(
            topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["routed_model"] == "Qwen3.8-27B"
        assert row["answering_backend"] == "local-coder"

        payload["attempts"] = []
        assert await runner.project_event(
            topic, payload, MessageMeta(partition=0, offset=1, fallback_id=cid)
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["routed_model"] == "Qwen3.8-27B"
        assert row["answering_backend"] == "local-coder"
