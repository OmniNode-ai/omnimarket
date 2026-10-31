# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The baseline a local store's newest metering row was folded under (OMN-20557).

Each ``onex delegate`` folds its tenant's rows under the baseline that run
resolved: the caller's session model when the request named one, else the
fixed default. ``onex metering`` with no ``--baseline`` reads the row the
newest fold wrote, so a developer's session model is the baseline it shows
right after a delegation from that session. Reads only; never folds or writes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_summary import (
    resolve_metering_baseline_model,
)

__all__ = ["newest_folded_baseline", "served_baseline"]


def newest_folded_baseline(
    db_path: Path,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
) -> str | None:
    """The baseline of the window's most recently folded row, or None."""
    if not db_path.exists():
        return None
    rows = SqliteDatabaseAdapter(db_path).query(
        "metering_summary",
        {
            "tenant_id": tenant_id,
            "window_kind": window_kind,
            "window_start": window_start,
        },
    )
    folded = [
        (datetime.fromisoformat(str(row["as_of"])), str(row["baseline_model"]))
        for row in rows
        if row.get("as_of") and row.get("baseline_model")
    ]
    return max(folded)[1] if folded else None


def served_baseline(
    explicit: str | None,
    db_path: Path,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
) -> str:
    """The baseline ``onex metering`` reads: the named one, else the newest fold's.

    With no row folded yet, the baseline a run with no session model resolves.
    """
    return (
        explicit
        or newest_folded_baseline(db_path, tenant_id, window_kind, window_start)
        or resolve_metering_baseline_model().model
    )
