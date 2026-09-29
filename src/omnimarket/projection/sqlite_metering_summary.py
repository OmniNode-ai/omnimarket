# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local snapshot refresh and durable row lookup; no arithmetic lives here.

The SQLite database is one install's evidence store. ``tenant_id`` labels that
install's projection; the underlying legacy records do not carry tenant ids.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    store_rows,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import read_metering_records


def refresh_metering_summary(
    db_path: Path,
    tenant_id: str,
    baseline_model: str,
    now: datetime,
    *,
    days: frozenset[date] | None = None,
    include_fixtures: bool = False,
) -> tuple[ModelMeteringSummaryRow, ...]:
    """Refresh every recorded day and all-time, plus explicitly requested days."""
    records = read_metering_records(
        db_path=db_path, window_end=now, include_fixtures=include_fixtures
    )
    requested_days = days
    if days is not None:
        requested_days = days | {r.occurred_at.astimezone(UTC).date() for r in records}
    result = HandlerProjectionMeteringSummary().handle(
        ModelMeteringSummaryFoldRequest(
            tenant_id=tenant_id,
            records=records,
            baseline=resolve_baseline(baseline_model),
            baseline_model=baseline_model,
            as_of=now,
            days=requested_days,
        )
    )
    store_rows(SqliteDatabaseAdapter(db_path), result.rows)
    return result.rows


def read_summary_row(
    db_path: Path,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
    baseline_model: str,
) -> ModelMeteringSummaryRow | None:
    """Read one complete snapshot. A missing file is not created by lookup."""
    if not db_path.exists():
        return None
    rows = SqliteDatabaseAdapter(db_path).query(
        "metering_summary",
        {
            "tenant_id": tenant_id,
            "window_kind": window_kind,
            "window_start": window_start,
            "baseline_model": baseline_model,
        },
    )
    return ModelMeteringSummaryRow.model_validate(rows[0]) if rows else None
