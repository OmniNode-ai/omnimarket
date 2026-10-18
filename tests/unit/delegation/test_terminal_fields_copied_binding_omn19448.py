# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: copying a declared gate cause differs from inferring one."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("store", ["inmemory", "sqlite"])
@pytest.mark.parametrize("event_type", ["delegation-failed", "delegate-skill-failed"])
@pytest.mark.parametrize("cause", ["quality_gate_refused", None])
def test_quality_gate_refused_is_copied_only_when_declared(
    tmp_path: Path, store: str, event_type: str, cause: str | None
) -> None:
    db = (
        SqliteDatabaseAdapter(tmp_path / "terminal.db")
        if store == "sqlite"
        else InmemoryDatabaseAdapter()
    )
    seed_tenant_registry(db)
    correlation_id = str(uuid4())
    payload: dict[str, object] = {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "_db": db,
        "_event_type": event_type,
        "correlation_id": correlation_id,
        "task_type": "test",
        "status": "failed",
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

    result = HandlerProjectionDelegation().handle(payload)

    assert result["rows_upserted"] == 1
    (row,) = db.query(TABLE, {"correlation_id": correlation_id})
    assert row.get("terminal_failure_cause") == cause
    assert row["operational_outcome"] == "quality_rejected"
    assert row["content_verdict"] == "unusable"
