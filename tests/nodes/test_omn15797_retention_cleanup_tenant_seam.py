# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15797 AC3 -- NodeRetentionCleanup runs its deletes under the tenant GUC.

``agent_routing_decisions`` is RLS-covered (``node_projection_routing_decision``
migration ``0022_agent_routing_decisions_tenant_id_and_rls.sql``). With
``app.tenant_id`` unset the policy predicate is NULL, so every ``DELETE``
matches zero rows and every dry-run ``COUNT(*)`` returns zero -- and this
handler would report ``status: ok, total_deleted: 0`` forever while the table
grew without bound. Nothing raises; nothing is observable. That is the same
silent-zero this ticket exists to make unrepresentable.

The load-bearing assertion is ORDER: ``set_config`` must be the FIRST statement
on the connection's transaction. ``is_local=true`` scopes the GUC to the
current transaction, so a call issued after the first DELETE would leave that
DELETE blinded, and a call outside the transaction would evaporate before any
of them ran (the OMN-15306 silent no-op shape on the writer side).

Also covers this node's declared terminal event, which had no test asserting it.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import psycopg2  # type: ignore[import-untyped]
import pytest
import yaml

from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    EnumCleanupStatus,
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_retention_cleanup"
    / "contract.yaml"
)
_TERMINAL_EVENT = "onex.evt.omnimarket.retention-cleanup-completed.v1"


class _RecordingCursor:
    """Records every statement, answering COUNT(*) with a fixed row."""

    def __init__(self, recorded: list[str]) -> None:
        self._recorded = recorded
        self.rowcount = 0

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        self._recorded.append(str(query))

    def fetchone(self) -> tuple[int]:
        return (0,)


class _RecordingConnection:
    def __init__(self, recorded: list[str]) -> None:
        self._recorded = recorded
        self.autocommit = True
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._recorded)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


@pytest.fixture
def recorded_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(
        psycopg2,
        "connect",
        lambda *_args, **_kwargs: _RecordingConnection(recorded),
    )
    return recorded


def test_set_config_is_the_first_statement(recorded_statements: list[str]) -> None:
    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db", dry_run=True)
    )

    assert result.status is EnumCleanupStatus.DRY_RUN
    assert recorded_statements, "handler issued no statements at all"
    first = recorded_statements[0]
    assert "set_config" in first, (
        "the tenant GUC must be set before any retention statement; first "
        f"statement was {first!r}"
    )
    # Every subsequent statement rides the same transaction that GUC scopes.
    assert any("agent_routing_decisions" in stmt for stmt in recorded_statements[1:])


def test_tenant_guc_is_the_rls_policy_guc() -> None:
    """Pins the GUC name against the policy's own ``current_setting`` key --
    a near-miss name would set a GUC no policy ever reads."""
    assert TENANT_GUC == "app.tenant_id"


def test_contract_declares_the_terminal_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert contract["terminal_event"] == _TERMINAL_EVENT
    assert _TERMINAL_EVENT in contract["event_bus"]["publish_topics"]


def test_no_db_url_short_circuits_before_resolving_a_tenant(
    recorded_statements: list[str],
) -> None:
    """The no-DB path must stay a clean skip, not become a tenant failure."""
    result = NodeRetentionCleanup().handle(RetentionCleanupRequest(db_url=""))

    assert result.status is EnumCleanupStatus.NO_DB
    assert recorded_statements == []
