# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 -- declared, batched, scheduled retention in NodeRetentionCleanup.

omninode_internal.consumer_flow_windows grew to 48.3 million rows and 32 GB on
the lab Postgres with nothing pruning it, and the board's consumer-flow probe
timed out reading it. Its readers need minutes of history (the board's live
read is the last 10 minutes). These tests pin the retention node that now
prunes it:

* the contract registers the table, schema-qualified, with a bounded batch;
* a batched target deletes in bounded batches, one committed transaction each,
  with the tenant GUC set first in every batch, until a short batch;
* a contract that names a ledger, credential, tenant registry or migration
  history table is refused at load;
* one failing target does not stop the others;
* the runtime tick runs the cleanup once per declared interval.
"""

from __future__ import annotations

import datetime as dt
import importlib
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_retention_cleanup as mod,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    EnumCleanupStatus,
    NodeRetentionCleanup,
    RetentionCleanupRequest,
    RetentionTarget,
)

psycopg2 = importlib.import_module("psycopg2")

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_retention_cleanup"
    / "contract.yaml"
)


class _Cursor:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self._conn.log.append(text)
        for name in self._conn.fail_on:
            if name in text and "set_config" not in text:
                raise RuntimeError(f"relation {name} does not exist")
        if "DELETE" in text:
            self.rowcount = (
                self._conn.delete_counts.pop(0) if self._conn.delete_counts else 0
            )

    def fetchone(self) -> tuple[int]:
        return (0,)


class _Connection:
    def __init__(self, delete_counts: list[int], fail_on: tuple[str, ...]) -> None:
        self.log: list[str] = []
        self.delete_counts = delete_counts
        self.fail_on = fail_on
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.log.append("COMMIT")
        self.commits += 1

    def rollback(self) -> None:
        self.log.append("ROLLBACK")
        self.rollbacks += 1

    def close(self) -> None:
        return None


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    targets: list[RetentionTarget],
    delete_counts: list[int],
    fail_on: tuple[str, ...] = (),
) -> _Connection:
    conn = _Connection(delete_counts, fail_on)
    monkeypatch.setattr(psycopg2, "connect", lambda *_a, **_k: conn)
    monkeypatch.setattr(mod, "_RETENTION_TARGETS", targets)
    return conn


_CFW = RetentionTarget(
    schema_name="omninode_internal",
    table="consumer_flow_windows",
    column="window_start",
    retention_days=7,
    batch_size=100,
    batch_order_column="projection_cursor",
)


def test_contract_registers_consumer_flow_windows() -> None:
    targets = {(t.schema_name, t.table): t for t in mod._load_targets(_CONTRACT)}
    cfw = targets[("omninode_internal", "consumer_flow_windows")]
    assert cfw.column == "window_start"
    assert 1 <= cfw.retention_days <= 30
    assert cfw.batch_size > 0
    assert cfw.max_batches > 0
    assert cfw.batch_order_column == "projection_cursor"


def test_batched_target_commits_each_bounded_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _patch(monkeypatch, [_CFW], delete_counts=[100, 100, 7])

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db")
    )

    assert result.status is EnumCleanupStatus.OK
    assert result.total_deleted == 207
    assert result.tables[0].table == "omninode_internal.consumer_flow_windows"
    assert result.tables[0].batches == 3
    deletes = [s for s in conn.log if "DELETE" in s]
    assert len(deletes) == 3, "a short batch must end the loop"
    assert all("LIMIT" in s and "omninode_internal" in s for s in deletes)
    assert all("ORDER BY" in s and "projection_cursor" in s for s in deletes)
    # Each batch is its own transaction: GUC first, then the DELETE, then COMMIT.
    for i, stmt in enumerate(conn.log):
        if "DELETE" in stmt:
            assert "set_config" in conn.log[i - 1]
            assert conn.log[i + 1] == "COMMIT"


def test_batched_target_stops_at_max_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    capped = _CFW.model_copy(update={"max_batches": 2})
    conn = _patch(monkeypatch, [capped], delete_counts=[100, 100, 100, 100])

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db")
    )

    assert result.total_deleted == 200
    assert len([s for s in conn.log if "DELETE" in s]) == 2


def test_dry_run_counts_and_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _patch(monkeypatch, [_CFW], delete_counts=[100])

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db", dry_run=True)
    )

    assert result.status is EnumCleanupStatus.DRY_RUN
    assert not [s for s in conn.log if "DELETE" in s]
    assert conn.commits == 0


def test_failing_target_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = RetentionTarget(
        table="agent_actions", column="created_at", retention_days=30
    )
    conn = _patch(
        monkeypatch, [missing, _CFW], delete_counts=[5], fail_on=("agent_actions",)
    )

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db")
    )

    by_table = {t.table: t for t in result.tables}
    assert "does not exist" in by_table["agent_actions"].error
    assert by_table["omninode_internal.consumer_flow_windows"].rows_deleted == 5
    assert conn.rollbacks >= 1


@pytest.mark.parametrize(
    "table",
    [
        "event_ledger",
        "work_ledger_rows",
        "credential_store",
        "tenant_registry",
        "schema_migrations",
    ],
)
def test_protected_tables_are_refused(tmp_path: Path, table: str) -> None:
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        yaml.safe_dump(
            {
                "retention_policy": {
                    "targets": [
                        {"table": table, "column": "created_at", "retention_days": 30}
                    ]
                }
            }
        )
    )
    with pytest.raises(RuntimeError, match="protected"):
        mod._load_targets(contract, allowed=frozenset({table}))


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def test_runtime_tick_runs_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [_CFW], delete_counts=[3, 4])
    env = mod.contract_schedule().database_url_env
    monkeypatch.setenv(env, "postgresql://unused/db")
    interval = mod.contract_schedule().run_interval_seconds
    node = NodeRetentionCleanup()
    t0 = dt.datetime(2026, 10, 16, 23, 0, tzinfo=dt.UTC)

    first = node.handle(_tick(t0))
    skipped = node.handle(_tick(t0 + dt.timedelta(seconds=interval - 1)))
    second = node.handle(_tick(t0 + dt.timedelta(seconds=interval)))

    assert first is not None
    assert first.total_deleted == 3
    assert skipped is None
    assert second is not None
    assert second.total_deleted == 4


def test_runtime_tick_without_database_reports_no_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(mod.contract_schedule().database_url_env, raising=False)
    result = NodeRetentionCleanup().handle(
        _tick(dt.datetime(2026, 10, 16, tzinfo=dt.UTC))
    )
    assert result is not None
    assert result.status is EnumCleanupStatus.NO_DB
