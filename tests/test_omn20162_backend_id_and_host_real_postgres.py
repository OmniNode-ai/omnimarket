# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20162: the row stores the accepting attempt's backend_id and host.

Real Postgres twin of ``tests/unit/projection/test_delegation_backend_id_and_host_omn20162.py``:
the disposable schema is migrated with this node's full migration set (so
migration 0054 is applied), a canonical terminal is projected through the REAL
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

_SELECT = (
    "SELECT backend_id, host, answering_backend FROM delegation_events "
    "WHERE correlation_id = $1"
)


@pytest.mark.integration
class TestTheRowCarriesBackendIdAndHost:
    async def test_a_terminal_stores_backend_id_and_host(self) -> None:
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload["route"] = "local-qwen"
        payload["backend_id"] = "local-b"
        payload["host"] = "h202"
        cid = str(terminal.correlation_id)

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["backend_id"] == "local-b"
            assert row["host"] == "h202"
            assert row["answering_backend"] == "local-qwen"

    async def test_a_terminal_without_them_and_a_blank_one_store_nulls(self) -> None:
        bare = _quota_terminal()
        blank = _quota_terminal()
        blank_payload = _wire(blank)
        blank_payload["backend_id"] = "  "
        blank_payload["host"] = ""

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            for offset, payload, terminal in (
                (1, _wire(bare), bare),
                (2, blank_payload, blank),
            ):
                cid = str(terminal.correlation_id)
                assert await runner.project_event(
                    runner._topic_delegation_failed,
                    payload,
                    MessageMeta(partition=0, offset=offset, fallback_id=cid),
                )
                row = await admin_conn.fetchrow(_SELECT, cid)
                assert row is not None
                assert row["backend_id"] is None
                assert row["host"] is None
