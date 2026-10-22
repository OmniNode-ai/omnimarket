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


# ---------------------------------------------------------------------------
# Run budget: the handler runs on the runtime's event loop, so a backlog must
# be drained across ticks and not in one blocking call.
# ---------------------------------------------------------------------------


def _clock(monkeypatch: pytest.MonkeyPatch, step: float) -> None:
    now = [0.0]

    def tick() -> float:
        now[0] += step
        return now[0]

    monkeypatch.setattr(mod, "_monotonic", tick)


def test_contract_declares_a_short_run_budget() -> None:
    schedule = mod.contract_schedule(_CONTRACT)
    assert 0 < schedule.max_run_seconds <= 10
    assert schedule.backlog_retry_seconds < schedule.run_interval_seconds


def test_run_budget_stops_after_the_first_batch_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _patch(monkeypatch, [_CFW], delete_counts=[100] * 20)
    _clock(monkeypatch, step=1.0)

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db", max_run_seconds=3)
    )

    # The deadline is read once at the start (t=1 -> 4); the check after each
    # batch reads t=2, 3, 4, so the third batch is the one that ends the run.
    assert result.tables[0].batches == 3
    assert result.total_deleted == 300
    assert result.tables[0].backlog_remaining is True
    assert result.backlog_remaining is True
    assert len([s for s in conn.log if "DELETE" in s]) == 3
    assert conn.log[-1] == "COMMIT", "the last batch is committed, never abandoned"


def test_run_budget_already_spent_still_makes_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, [_CFW], delete_counts=[100] * 5)
    _clock(monkeypatch, step=10.0)

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db", max_run_seconds=1)
    )

    assert result.tables[0].batches == 1
    assert result.total_deleted == 100


def test_drained_table_reports_no_backlog_even_after_full_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, [_CFW], delete_counts=[100, 100, 7])
    _clock(monkeypatch, step=0.001)

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db", max_run_seconds=60)
    )

    assert result.tables[0].batches == 3
    assert result.tables[0].backlog_remaining is False
    assert result.backlog_remaining is False


def test_no_budget_runs_to_max_batches_and_reports_the_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capped = _CFW.model_copy(update={"max_batches": 2})
    _patch(monkeypatch, [capped], delete_counts=[100] * 4)

    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(db_url="postgresql://unused/db")
    )

    assert result.tables[0].batches == 2
    assert result.tables[0].backlog_remaining is True


def test_tick_resumes_a_backlog_after_the_retry_wait_not_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, [_CFW], delete_counts=[100, 100, 100, 3, 50])
    _clock(monkeypatch, step=3.0)
    monkeypatch.setenv(mod.contract_schedule().database_url_env, "postgresql://u/db")
    schedule = mod.contract_schedule()
    node = NodeRetentionCleanup()
    t0 = dt.datetime(2026, 10, 16, 23, 0, tzinfo=dt.UTC)
    retry = dt.timedelta(seconds=schedule.backlog_retry_seconds)

    first = node.handle(_tick(t0))
    too_soon = node.handle(_tick(t0 + retry - dt.timedelta(seconds=1)))
    second = node.handle(_tick(t0 + retry))
    after_drain = node.handle(_tick(t0 + 2 * retry))
    next_interval = node.handle(
        _tick(t0 + retry + dt.timedelta(seconds=schedule.run_interval_seconds))
    )

    assert first is not None
    assert first.backlog_remaining is True, "budget ended the first run on a full batch"
    assert too_soon is None
    assert second is not None
    assert second.backlog_remaining is False, "a short batch drains the table"
    assert after_drain is None, "drained: back to the full interval"
    assert next_interval is not None


# ---------------------------------------------------------------------------
# Off the loop: one batch of the consumer_flow_windows delete was measured at
# over 34 s on the lab dev lane, and every run takes at least one batch, so the
# budget alone cannot keep the event loop answering. The runtime entry point
# runs the run in a worker thread.
# ---------------------------------------------------------------------------


def test_runtime_entry_point_is_handle_async() -> None:
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _resolve_effective_handle_method,
    )

    node = NodeRetentionCleanup()
    resolved = _resolve_effective_handle_method(node)
    assert resolved is not None
    assert getattr(resolved, "__name__", "") == "handle_async"


def test_scheduled_run_leaves_the_event_loop_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    _patch(monkeypatch, [_CFW], delete_counts=[3])
    monkeypatch.setenv(mod.contract_schedule().database_url_env, "postgresql://u/db")
    release = threading.Event()
    seen: dict[str, object] = {}
    real_run = NodeRetentionCleanup._run

    def blocking_run(
        self: NodeRetentionCleanup, request: RetentionCleanupRequest
    ) -> mod.RetentionCleanupResult:
        seen["thread"] = threading.current_thread()
        # Holds the run the way a slow batch does; the loop must keep going.
        assert release.wait(timeout=10), "the loop never ran while the run held"
        return real_run(self, request)

    monkeypatch.setattr(NodeRetentionCleanup, "_run", blocking_run)

    async def scenario() -> mod.RetentionCleanupResult | None:
        node = NodeRetentionCleanup()
        run = asyncio.create_task(
            node.handle_async(_tick(dt.datetime(2026, 10, 21, 20, 0, tzinfo=dt.UTC)))
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "thread" in seen:
                break
        seen["loop_ran_while_held"] = "thread" in seen and not run.done()
        release.set()
        return await run

    result = asyncio.run(scenario())

    assert result is not None
    assert result.total_deleted == 3
    assert seen["thread"] is not threading.main_thread()
    assert seen["loop_ran_while_held"] is True


def test_tick_during_a_run_is_skipped_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    _patch(monkeypatch, [_CFW], delete_counts=[3, 3])
    monkeypatch.setenv(mod.contract_schedule().database_url_env, "postgresql://u/db")
    release = threading.Event()
    started = threading.Event()
    runs: list[int] = []
    real_run = NodeRetentionCleanup._run

    def blocking_run(
        self: NodeRetentionCleanup, request: RetentionCleanupRequest
    ) -> mod.RetentionCleanupResult:
        runs.append(1)
        started.set()
        assert release.wait(timeout=10)
        return real_run(self, request)

    monkeypatch.setattr(NodeRetentionCleanup, "_run", blocking_run)
    t0 = dt.datetime(2026, 10, 21, 20, 0, tzinfo=dt.UTC)
    late = t0 + dt.timedelta(seconds=mod.contract_schedule().run_interval_seconds)

    async def scenario() -> tuple[object, object]:
        node = NodeRetentionCleanup()
        first = asyncio.create_task(node.handle_async(_tick(t0)))
        while not started.is_set():
            await asyncio.sleep(0.01)
        # Due by the interval, but the first run still holds its thread.
        second = await node.handle_async(_tick(late))
        release.set()
        return await first, second

    first, second = asyncio.run(scenario())

    assert first is not None
    assert second is None, "a tick during a run must not start a second run"
    assert runs == [1]
