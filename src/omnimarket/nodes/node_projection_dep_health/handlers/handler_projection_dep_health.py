# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionDepHealth — project dep-health-sweep-completed events to DB.

Consumes onex.evt.omnimarket.dep-health-sweep-completed.v1 and UPSERTs
findings into dep_health_findings table. Keyed by (run_id, finding_type,
file_path, symbol). Idempotent: projecting the same event twice leaves
exactly N rows, not 2N.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_completed_event import (
    ModelDepHealthSweepCompletedEvent,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "dep_health_findings"
CONFLICT_KEY = "run_id,finding_type,file_path,symbol"


class ModelProjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionDepHealth:
    """Project dep-health-sweep-completed events into dep_health_findings table."""

    def project(
        self,
        event: ModelDepHealthSweepCompletedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT all findings from a sweep-completed event."""
        count = 0
        captured_at = event.captured_at.isoformat()
        for finding in event.findings:
            row: dict[str, object] = {
                "run_id": event.run_id,
                "finding_type": finding.finding_type,
                "severity": finding.severity,
                "repo": finding.repo,
                "file_path": finding.file_path or "",
                "symbol": finding.symbol or "",
                "detail": finding.detail,
                "rule_id": finding.rule_id,
                "rule_version": finding.rule_version,
                "captured_at": captured_at,
            }
            ok = db.upsert(TABLE, CONFLICT_KEY, row)
            if ok:
                count += 1
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionDepHealth",
    "ModelProjectionResult",
]
