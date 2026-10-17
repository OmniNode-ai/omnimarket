# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeRetentionCleanup — Prunes stale projection table records.

Retention targets and windows are declared in contract.yaml. This handler
reads those declarations at startup — changing retention policy requires a
contract change, not a code change.

ONEX node type: EFFECT — performs database writes (side effects).

In dry-run mode, estimates rows to delete without executing DELETE statements.

Tenant seam (OMN-15797 AC3): ``agent_routing_decisions`` is RLS-covered
(``node_projection_routing_decision`` migration
``0022_agent_routing_decisions_tenant_id_and_rls.sql``). With ``app.tenant_id``
unset, the policy predicate is NULL and every ``DELETE`` matches zero rows and
every dry-run ``COUNT(*)`` reports zero — retention would report "OK, deleted
0 rows" forever while the table grew without bound. The GUC is therefore set
as the first statement of the same transaction the targets run in.

Setting it is safe for the identity retention actually runs as today: an
owner/superuser connection bypasses RLS entirely and is unaffected, while a
non-superuser connection goes from deleting nothing to deleting its tenant's
aged rows. There is no configuration under which this narrows a delete that
was previously working.

Batched targets (OMN-17427): a target with ``batch_size`` deletes at most that
many rows per statement, each batch its own committed transaction with the
tenant GUC set first, until a short batch or ``max_batches``. A large table is
pruned in short transactions instead of one long lock-holding DELETE. Every
target runs in its own transaction, so a target that fails (a table absent on
this database) is rolled back and reported without aborting the others.

A target naming a ledger, credential, tenant registry or migration history
table is refused at load: retention never removes those rows.

Schedule (OMN-17427): the platform runtime tick runs the cleanup once per
``schedule.run_interval_seconds`` against the database named by the
contract's ``schedule.database_url_env``, and the run's result is published on
the completed topic, one event per run with rows removed per table.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, overload

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retention targets — loaded from contract.yaml at import time.
# Table names owned by their respective projection migration files.
# Update the allowlist only when adding a new projection table.
# ---------------------------------------------------------------------------

_ALLOWED_TABLES: frozenset[str] = frozenset(
    [
        "agent_actions",
        "agent_routing_decisions",
        "omninode_internal.consumer_flow_windows",
    ]
)

# Retention never removes ledger, credential, tenant registry or migration
# history rows, whatever a contract declares.
_PROTECTED_MARKERS: tuple[str, ...] = (
    "ledger",
    "credential",
    "secret",
    "tenant",
    "migration",
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class RetentionTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_name: str = Field(default="public", alias="schema")
    table: str
    column: str
    retention_days: int
    batch_size: int = Field(default=0, ge=0)
    max_batches: int = Field(default=1000, ge=1)
    # An indexed column that rises with insertion. Each batch walks it from
    # the oldest end, so a table with no index on ``column`` is not scanned
    # whole for every batch.
    batch_order_column: str = ""
    topic_column: str = ""
    high_volume_topics: tuple[str, ...] = Field(default_factory=tuple)
    high_volume_retention_days: int = 0
    never_store_topics: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def qualified_name(self) -> str:
        if self.schema_name == "public":
            return self.table
        return f"{self.schema_name}.{self.table}"


class RetentionSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(ge=60)
    database_url_env: str
    dry_run: bool = False


def contract_schedule(contract_path: Path = _CONTRACT_PATH) -> RetentionSchedule:
    raw = yaml.safe_load(contract_path.read_text()) or {}
    return RetentionSchedule.model_validate(
        (raw.get("retention_policy") or {})["schedule"]
    )


def _load_targets(
    contract_path: Path = _CONTRACT_PATH,
    allowed: frozenset[str] = _ALLOWED_TABLES,
) -> list[RetentionTarget]:
    """Load retention targets from the node's contract.yaml.

    Raises RuntimeError when the contract is missing or the retention_policy
    block is absent — retention must always be contract-driven.
    """
    if not contract_path.exists():
        msg = f"contract.yaml not found at {contract_path}"
        raise RuntimeError(msg)
    raw = yaml.safe_load(contract_path.read_text())
    policy = (raw or {}).get("retention_policy") or {}
    raw_targets = policy.get("targets") or []
    if not raw_targets:
        msg = (
            f"contract.yaml at {contract_path} does not declare a "
            "retention_policy.targets block"
        )
        raise RuntimeError(msg)
    targets: list[RetentionTarget] = []
    for entry in raw_targets:
        table = entry.get("table", "")
        schema = entry.get("schema", "public") or "public"
        qualified = table if schema == "public" else f"{schema}.{table}"
        if any(m in table.lower() for m in _PROTECTED_MARKERS):
            msg = (
                f"retention target {qualified} is a protected table (ledger, "
                "credential, tenant registry or migration history); refused"
            )
            raise RuntimeError(msg)
        if qualified not in allowed:
            _log.warning("Table %s not in allowlist — skipping", qualified)
            continue
        targets.append(
            RetentionTarget(
                schema_name=schema,
                table=table,
                column=entry.get("column", "created_at"),
                retention_days=int(entry.get("retention_days", 14)),
                topic_column=entry.get("topic_column", "") or "",
                high_volume_topics=tuple(entry.get("high_volume_topics", []) or []),
                high_volume_retention_days=int(
                    entry.get("high_volume_retention_days", 0) or 0
                ),
                never_store_topics=tuple(entry.get("never_store_topics", []) or []),
                batch_size=int(entry.get("batch_size", 0) or 0),
                max_batches=int(entry.get("max_batches", 1000) or 1000),
                batch_order_column=entry.get("batch_order_column", "") or "",
            )
        )
    return targets


_RETENTION_TARGETS: list[RetentionTarget] = _load_targets()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumCleanupStatus(StrEnum):
    OK = "ok"
    DRY_RUN = "dry_run"
    NO_DB = "no_db"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RetentionCleanupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool = Field(
        default=False, description="Estimate deletes without executing"
    )
    db_url: str = Field(
        default="",
        description="PostgreSQL connection URL; empty = skip DB operations",
    )


class RetentionTableResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    rows_deleted: int = Field(default=0)
    batches: int = Field(default=0)
    estimated: bool = Field(default=False, description="True when dry_run=True")
    error: str = Field(default="")


class RetentionCleanupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumCleanupStatus
    tables: list[RetentionTableResult] = Field(default_factory=list)
    total_deleted: int = Field(default=0)
    dry_run: bool = Field(default=False)
    message: str = Field(default="")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeRetentionCleanup:
    """Prunes stale records from declared projection tables.

    Applies three deletion passes per target table when contract specifies them:
      1. never_store_topics — delete ALL rows regardless of age
      2. high_volume_topics — delete rows older than high_volume_retention_days
      3. general retention — delete remaining rows older than retention_days

    In dry-run mode (--dry-run), runs COUNT(*) queries instead of DELETE and
    rolls back without modifying any data.
    """

    def __init__(self) -> None:
        # In-process stamp for the runtime-tick schedule, the idiom
        # node_consumer_flow_prune_effect uses: the stamp moves only when a
        # run proceeds, so a skipped tick never resets the interval.
        self._last_scheduled_run: dt.datetime | None = None

    @overload
    def handle(self, request: RetentionCleanupRequest) -> RetentionCleanupResult: ...

    @overload
    def handle(self, request: ModelRuntimeTick) -> RetentionCleanupResult | None: ...

    def handle(
        self, request: RetentionCleanupRequest | ModelRuntimeTick
    ) -> RetentionCleanupResult | None:
        if isinstance(request, ModelRuntimeTick):
            gated = self._gate_scheduled_run(request)
            if gated is None:
                return None
            request = gated
        return self._run(request)

    def _gate_scheduled_run(
        self, tick: ModelRuntimeTick
    ) -> RetentionCleanupRequest | None:
        schedule = contract_schedule()
        now = tick.now.astimezone(dt.UTC)
        last = self._last_scheduled_run
        if last is not None and (now - last) < dt.timedelta(
            seconds=schedule.run_interval_seconds
        ):
            return None
        self._last_scheduled_run = now
        return RetentionCleanupRequest(
            dry_run=schedule.dry_run,
            db_url=os.environ.get(schedule.database_url_env, ""),
        )

    def _run(self, request: RetentionCleanupRequest) -> RetentionCleanupResult:
        if not request.db_url:
            return RetentionCleanupResult(
                status=EnumCleanupStatus.NO_DB,
                message="No db_url provided — skipping retention cleanup",
                dry_run=request.dry_run,
            )

        try:
            import psycopg2  # type: ignore[import-untyped]
            from psycopg2 import sql as pgsql
        except ImportError:
            return RetentionCleanupResult(
                status=EnumCleanupStatus.ERROR,
                message="psycopg2 not installed — cannot run retention cleanup",
                dry_run=request.dry_run,
            )

        table_results: list[RetentionTableResult] = []
        total_deleted = 0

        # Resolved before connecting so a refused run issues no SQL at all.
        tenant = resolve_rls_read_tenant(None, table="retention_cleanup")

        try:
            conn = psycopg2.connect(request.db_url)
            conn.autocommit = False
            try:
                for target in _RETENTION_TARGETS:
                    if target.batch_size and not request.dry_run:
                        result = self._run_batched(conn, pgsql, target, tenant)
                    else:
                        result = self._run_single(
                            conn, pgsql, target, tenant, dry_run=request.dry_run
                        )
                    table_results.append(result)
                    total_deleted += result.rows_deleted
            finally:
                conn.close()
        except Exception as exc:
            return RetentionCleanupResult(
                status=EnumCleanupStatus.ERROR,
                message=f"Database error: {exc}",
                dry_run=request.dry_run,
                tables=table_results,
                total_deleted=total_deleted,
            )

        return RetentionCleanupResult(
            status=EnumCleanupStatus.DRY_RUN
            if request.dry_run
            else EnumCleanupStatus.OK,
            tables=table_results,
            total_deleted=total_deleted,
            dry_run=request.dry_run,
            message=(
                f"Dry run: would delete {total_deleted} rows"
                if request.dry_run
                else f"Deleted {total_deleted} rows"
            ),
        )

    def _run_single(
        self,
        conn: Any,
        pgsql: Any,
        target: RetentionTarget,
        tenant: str,
        *,
        dry_run: bool,
    ) -> RetentionTableResult:
        """One transaction for the target: GUC first, every pass, then commit.

        Dry run and a failed target roll back, so neither changes anything.
        """
        with conn.cursor() as cur:
            # First statement of this transaction: set_config's is_local=true
            # scopes the GUC to it, and every pass of the target runs inside it.
            cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant))
            result = self._run_target(cur, pgsql, target, dry_run=dry_run)
        if dry_run or result.error:
            conn.rollback()
        else:
            conn.commit()
        return result

    def _run_batched(
        self, conn: Any, pgsql: Any, target: RetentionTarget, tenant: str
    ) -> RetentionTableResult:
        """Delete aged rows ``batch_size`` at a time, one committed transaction each."""
        order = (
            pgsql.SQL(" ORDER BY {}").format(
                pgsql.Identifier(target.batch_order_column)
            )
            if target.batch_order_column
            else pgsql.SQL("")
        )
        query = pgsql.SQL(
            "DELETE FROM {tbl} WHERE ctid = ANY(ARRAY("
            "SELECT ctid FROM {tbl} WHERE {col} < NOW() - INTERVAL %s{order} LIMIT %s))"
        ).format(
            tbl=pgsql.Identifier(target.schema_name, target.table),
            col=pgsql.Identifier(target.column),
            order=order,
        )
        interval = f"{target.retention_days} days"
        total = batches = 0
        try:
            while batches < target.max_batches:
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant))
                    cur.execute(query, [interval, target.batch_size])
                    n = int(cur.rowcount or 0)
                conn.commit()
                batches += 1
                total += n
                if n < target.batch_size:
                    break
        except Exception as exc:
            conn.rollback()
            return RetentionTableResult(
                table=target.qualified_name,
                rows_deleted=total,
                batches=batches,
                error=str(exc),
            )
        return RetentionTableResult(
            table=target.qualified_name, rows_deleted=total, batches=batches
        )

    def _run_target(
        self,
        cur: Any,
        pgsql: Any,
        target: RetentionTarget,
        dry_run: bool,
    ) -> RetentionTableResult:
        try:
            total = 0
            topic_col = target.topic_column
            if topic_col and target.never_store_topics:
                total += self._delete_never_store(
                    cur,
                    pgsql,
                    target,
                    topic_col,
                    target.never_store_topics,
                    dry_run,
                )
            if (
                topic_col
                and target.high_volume_topics
                and target.high_volume_retention_days > 0
            ):
                total += self._delete_aged(
                    cur,
                    pgsql,
                    target,
                    target.column,
                    target.high_volume_retention_days,
                    topic_col=topic_col,
                    topics=target.high_volume_topics,
                    dry_run=dry_run,
                )
            total += self._delete_aged(
                cur,
                pgsql,
                target,
                target.column,
                target.retention_days,
                topic_col="",
                topics=(),
                dry_run=dry_run,
            )
            return RetentionTableResult(
                table=target.qualified_name,
                rows_deleted=total,
                estimated=dry_run,
            )
        except Exception as exc:
            return RetentionTableResult(
                table=target.qualified_name,
                rows_deleted=0,
                error=str(exc),
            )

    def _delete_never_store(
        self,
        cur: Any,
        pgsql: Any,
        target: RetentionTarget,
        topic_col: str,
        topics: tuple[str, ...],
        dry_run: bool,
    ) -> int:
        table = pgsql.Identifier(target.schema_name, target.table)
        if dry_run:
            query = pgsql.SQL("SELECT COUNT(*) FROM {} WHERE {} = ANY(%s)").format(
                table,
                pgsql.Identifier(topic_col),
            )
            cur.execute(query, [list(topics)])
            (count,) = cur.fetchone()
            return int(count)
        query = pgsql.SQL("DELETE FROM {} WHERE {} = ANY(%s)").format(
            table,
            pgsql.Identifier(topic_col),
        )
        cur.execute(query, [list(topics)])
        return int(cur.rowcount or 0)

    def _delete_aged(
        self,
        cur: Any,
        pgsql: Any,
        target: RetentionTarget,
        ts_col: str,
        retention_days: int,
        topic_col: str,
        topics: tuple[str, ...],
        dry_run: bool,
    ) -> int:
        interval = f"{retention_days} days"
        table = pgsql.Identifier(target.schema_name, target.table)
        if topic_col and topics:
            if dry_run:
                query = pgsql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE {} < NOW() - INTERVAL %s AND {} = ANY(%s)"
                ).format(
                    table,
                    pgsql.Identifier(ts_col),
                    pgsql.Identifier(topic_col),
                )
                cur.execute(query, [interval, list(topics)])
                (count,) = cur.fetchone()
                return int(count)
            query = pgsql.SQL(
                "DELETE FROM {} WHERE {} < NOW() - INTERVAL %s AND {} = ANY(%s)"
            ).format(
                table,
                pgsql.Identifier(ts_col),
                pgsql.Identifier(topic_col),
            )
            cur.execute(query, [interval, list(topics)])
            return int(cur.rowcount or 0)
        if dry_run:
            query = pgsql.SQL(
                "SELECT COUNT(*) FROM {} WHERE {} < NOW() - INTERVAL %s"
            ).format(
                table,
                pgsql.Identifier(ts_col),
            )
            cur.execute(query, [interval])
            (count,) = cur.fetchone()
            return int(count)
        query = pgsql.SQL("DELETE FROM {} WHERE {} < NOW() - INTERVAL %s").format(
            table,
            pgsql.Identifier(ts_col),
        )
        cur.execute(query, [interval])
        return int(cur.rowcount or 0)
