# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC2: the row stores the terminal's trace, model and backend.

Real Postgres twin of ``tests/unit/delegation/test_terminal_columns_omn19448.py``:
the disposable schema is migrated with this node's full migration set (so
migration 0052 is applied), a canonical terminal is projected through the REAL
``project_event()``, and each column is read back. The harness SKIPS without
``INTEGRATION_POSTGRES_PASSWORD`` and a reachable server.
"""

from __future__ import annotations

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _quota_terminal,
    _wire,
)

_TRACE_ID = "6f1d2e0b-9a57-4c1e-8f0e-3e1f9c0a4d99"
_SELECT = (
    "SELECT trace_id, routed_model, answering_backend, terminal_failure_cause, "
    "operational_outcome, content_verdict FROM delegation_events "
    "WHERE correlation_id = $1"
)


@pytest.mark.integration
class TestTheRowCarriesTraceAndRouting:
    async def test_a_failed_terminal_stores_deciding_rung_stop_reason(self) -> None:
        # OMN-19448: migration 0053 and the async canonical projection path.
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload["escalation_history"] = [{"finish_reason": "length", "truncated": True}]
        cid = str(terminal.correlation_id)

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(
                "SELECT finish_reason, truncated FROM delegation_events "
                "WHERE correlation_id = $1",
                cid,
            )
            assert row is not None
            assert row["finish_reason"] == "length"
            assert row["truncated"] is True

    async def test_a_failed_terminal_stores_trace_model_and_backend(self) -> None:
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload["trace_id"] = _TRACE_ID
        payload["route"] = "local-qwen"
        cid = str(terminal.correlation_id)

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["trace_id"] == _TRACE_ID
            assert row["routed_model"] == payload["model_used"]
            assert row["answering_backend"] == "local-qwen"
            assert row["terminal_failure_cause"] == "provider_quota_exhausted"
            assert row["operational_outcome"] == "provider_quota"

    async def test_a_terminal_without_trace_or_route_stores_nulls(self) -> None:
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload.pop("trace_id", None)
        payload.pop("route", None)
        cid = str(terminal.correlation_id)

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["trace_id"] is None
            assert row["answering_backend"] is None
