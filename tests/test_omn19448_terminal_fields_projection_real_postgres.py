# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: read declared terminal fields through the real async writer.

Uses the established INTEGRATION_POSTGRES_* harness and disposable migrated
schemas, so PostgreSQL enforces the projection's column types.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["canonical", "delegate-skill"])
@pytest.mark.parametrize("cause", ["quality_gate_refused", None])
async def test_quality_gate_refused_terminal_fields_are_copied_to_postgres(
    family: str, cause: str | None
) -> None:
    cid = str(uuid4())
    payload: dict[str, object] = {
        "tenant_id": "beta-business-proof",
        "status": "failed",
        "correlation_id": cid,
        "task_type": "test",
        "model_used": "test-model",
        "model_name": "test-model",
        "quality_passed": False,
        "quality_gate_passed": False,
        "failure_reason": "quality gate refused",
        "error_message": "quality gate refused",
        "operational_outcome": "quality_rejected",
        "content_verdict": "unusable",
    }
    if cause is not None:
        payload["terminal_failure_cause"] = cause

    async with _provisioned_runner() as (runner, connection, _schema):
        topic = (
            runner._topic_delegation_failed
            if family == "canonical"
            else runner._topic_delegate_skill_failed
        )
        assert await runner.project_event(
            topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
        )
        row = await connection.fetchrow(
            "SELECT terminal_failure_cause, operational_outcome, content_verdict "
            "FROM delegation_events WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["terminal_failure_cause"] == cause
        assert row["operational_outcome"] == "quality_rejected"
        assert row["content_verdict"] == "unusable"
