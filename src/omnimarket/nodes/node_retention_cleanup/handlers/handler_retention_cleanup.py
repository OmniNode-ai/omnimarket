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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retention targets — populated from contract.yaml at runtime.
# NEVER add table names here from external/user input; static allowlist only.
# ---------------------------------------------------------------------------

# Table names owned by their respective projection migration files.
# Update this allowlist only when adding a new projection table.
_ALLOWED_TABLES: frozenset[str] = frozenset(
    [
        "event_bus_events",
        "agent_actions",
        "agent_routing_decisions",
    ]
)

_DEFAULT_RETENTION_TARGETS: dict[str, tuple[str, int]] = {
    "event_bus_events": ("created_at", 14),
    "agent_actions": ("created_at", 30),
    "agent_routing_decisions": ("created_at", 30),
}


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

    In dry-run mode (--dry-run), runs COUNT(*) queries instead of DELETE
    to estimate how many rows would be removed, then returns without modifying
    any data.
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
                    for table_name, (
                        ts_column,
                        retention_days,
                    ) in _DEFAULT_RETENTION_TARGETS.items():
                        if table_name not in _ALLOWED_TABLES:
                            _log.warning(
                                "Table %s not in allowlist — skipping", table_name
                            )
                            continue
                        result = self._run_table(
                            cur,
                            pgsql,
                            table_name,
                            ts_column,
                            retention_days,
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

    def _run_table(
        self,
        cur: Any,
        pgsql: Any,
        table_name: str,
        ts_column: str,
        retention_days: int,
        dry_run: bool,
    ) -> RetentionTableResult:
        try:
            if dry_run:
                query = pgsql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE {} < NOW() - INTERVAL %s"
                ).format(
                    pgsql.Identifier(table_name),
                    pgsql.Identifier(ts_column),
                )
                cur.execute(query, [f"{retention_days} days"])
                (count,) = cur.fetchone()
                return RetentionTableResult(
                    table=table_name,
                    rows_deleted=count,
                    estimated=True,
                )
            query = pgsql.SQL("DELETE FROM {} WHERE {} < NOW() - INTERVAL %s").format(
                pgsql.Identifier(table_name),
                pgsql.Identifier(ts_column),
            )
            cur.execute(query, [f"{retention_days} days"])
            return RetentionTableResult(
                table=table_name,
                rows_deleted=cur.rowcount,
                estimated=False,
            )
        except Exception as exc:
            return RetentionTableResult(
                table=table_name,
                rows_deleted=0,
                error=str(exc),
            )
