# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeRetentionCleanup — Prunes stale projection table records.

Retention targets and windows are declared in contract.yaml. This handler
reads those declarations at startup — changing retention policy requires a
contract change, not a code change.

ONEX node type: EFFECT — performs database writes (side effects).

In dry-run mode, estimates rows to delete without executing DELETE statements.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retention targets — loaded from contract.yaml at import time.
# Table names owned by their respective projection migration files.
# Update the allowlist only when adding a new projection table.
# ---------------------------------------------------------------------------

_ALLOWED_TABLES: frozenset[str] = frozenset(
    [
        "event_bus_events",
        "agent_actions",
        "agent_routing_decisions",
    ]
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class RetentionTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    column: str
    retention_days: int
    topic_column: str = ""
    high_volume_topics: tuple[str, ...] = Field(default_factory=tuple)
    high_volume_retention_days: int = 0
    never_store_topics: tuple[str, ...] = Field(default_factory=tuple)


def _load_targets(contract_path: Path = _CONTRACT_PATH) -> list[RetentionTarget]:
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
        if table not in _ALLOWED_TABLES:
            _log.warning("Table %s not in allowlist — skipping", table)
            continue
        targets.append(
            RetentionTarget(
                table=table,
                column=entry.get("column", "created_at"),
                retention_days=int(entry.get("retention_days", 14)),
                topic_column=entry.get("topic_column", "") or "",
                high_volume_topics=tuple(entry.get("high_volume_topics", []) or []),
                high_volume_retention_days=int(
                    entry.get("high_volume_retention_days", 0) or 0
                ),
                never_store_topics=tuple(entry.get("never_store_topics", []) or []),
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

    def handle(self, request: RetentionCleanupRequest) -> RetentionCleanupResult:
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

        try:
            conn = psycopg2.connect(request.db_url)
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    for target in _RETENTION_TARGETS:
                        result = self._run_target(
                            cur,
                            pgsql,
                            target,
                            dry_run=request.dry_run,
                        )
                        table_results.append(result)
                        total_deleted += result.rows_deleted

                if not request.dry_run:
                    conn.commit()
                else:
                    conn.rollback()
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
                    target.table,
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
                    target.table,
                    target.column,
                    target.high_volume_retention_days,
                    topic_col=topic_col,
                    topics=target.high_volume_topics,
                    dry_run=dry_run,
                )
            total += self._delete_aged(
                cur,
                pgsql,
                target.table,
                target.column,
                target.retention_days,
                topic_col="",
                topics=(),
                dry_run=dry_run,
            )
            return RetentionTableResult(
                table=target.table,
                rows_deleted=total,
                estimated=dry_run,
            )
        except Exception as exc:
            return RetentionTableResult(
                table=target.table,
                rows_deleted=0,
                error=str(exc),
            )

    def _delete_never_store(
        self,
        cur: Any,
        pgsql: Any,
        table: str,
        topic_col: str,
        topics: tuple[str, ...],
        dry_run: bool,
    ) -> int:
        if dry_run:
            query = pgsql.SQL("SELECT COUNT(*) FROM {} WHERE {} = ANY(%s)").format(
                pgsql.Identifier(table),
                pgsql.Identifier(topic_col),
            )
            cur.execute(query, [list(topics)])
            (count,) = cur.fetchone()
            return int(count)
        query = pgsql.SQL("DELETE FROM {} WHERE {} = ANY(%s)").format(
            pgsql.Identifier(table),
            pgsql.Identifier(topic_col),
        )
        cur.execute(query, [list(topics)])
        return int(cur.rowcount or 0)

    def _delete_aged(
        self,
        cur: Any,
        pgsql: Any,
        table: str,
        ts_col: str,
        retention_days: int,
        topic_col: str,
        topics: tuple[str, ...],
        dry_run: bool,
    ) -> int:
        interval = f"{retention_days} days"
        if topic_col and topics:
            if dry_run:
                query = pgsql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE {} < NOW() - INTERVAL %s AND {} = ANY(%s)"
                ).format(
                    pgsql.Identifier(table),
                    pgsql.Identifier(ts_col),
                    pgsql.Identifier(topic_col),
                )
                cur.execute(query, [interval, list(topics)])
                (count,) = cur.fetchone()
                return int(count)
            query = pgsql.SQL(
                "DELETE FROM {} WHERE {} < NOW() - INTERVAL %s AND {} = ANY(%s)"
            ).format(
                pgsql.Identifier(table),
                pgsql.Identifier(ts_col),
                pgsql.Identifier(topic_col),
            )
            cur.execute(query, [interval, list(topics)])
            return int(cur.rowcount or 0)
        if dry_run:
            query = pgsql.SQL(
                "SELECT COUNT(*) FROM {} WHERE {} < NOW() - INTERVAL %s"
            ).format(
                pgsql.Identifier(table),
                pgsql.Identifier(ts_col),
            )
            cur.execute(query, [interval])
            (count,) = cur.fetchone()
            return int(count)
        query = pgsql.SQL("DELETE FROM {} WHERE {} < NOW() - INTERVAL %s").format(
            pgsql.Identifier(table),
            pgsql.Identifier(ts_col),
        )
        cur.execute(query, [interval])
        return int(cur.rowcount or 0)
