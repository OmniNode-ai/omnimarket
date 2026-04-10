"""Projection staleness prober.

For every projection table (specified by caller from DB query results):
- row_count == 0 with last_updated is None -> WARN (table likely empty/never populated)
- last_updated older than max_freshness_seconds -> WARN (>2x threshold -> FAIL)

Table specs are resolved by the handler via SSH psql query.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    ModelHealthFinding,
    ModelSubsystemResult,
    aggregate_status,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


class ModelProjectionSpec(BaseModel):
    """Spec for a single projection table to check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_name: str
    max_freshness_seconds: int = Field(default=3600, gt=0)
    row_count: int = Field(default=0, ge=0)
    last_updated: datetime | None = None


def probe_projections(
    specs: list[ModelProjectionSpec],
    now: datetime | None = None,
) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []
    now = now or datetime.now(UTC)
    checks = len(specs)

    for spec in specs:
        if spec.row_count == 0 and spec.last_updated is None:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.PROJECTIONS,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject=spec.table_name,
                    message=f"Projection table '{spec.table_name}' has 0 rows and no last_updated timestamp",
                    evidence="SELECT COUNT(*), MAX(updated_at) FROM " + spec.table_name,
                )
            )
            continue

        if spec.last_updated is not None:
            last_updated = (
                spec.last_updated
                if spec.last_updated.tzinfo is not None
                else spec.last_updated.replace(tzinfo=UTC)
            )
            age = (now - last_updated).total_seconds()
            if age > spec.max_freshness_seconds * 2:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.PROJECTIONS,
                        severity=EnumHealthFindingSeverity.FAIL,
                        subject=spec.table_name,
                        message=f"Projection '{spec.table_name}' last updated {age / 3600:.1f}h ago (max: {spec.max_freshness_seconds / 3600:.1f}h)",
                        evidence="SELECT MAX(updated_at) FROM " + spec.table_name,
                    )
                )
            elif age > spec.max_freshness_seconds:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.PROJECTIONS,
                        severity=EnumHealthFindingSeverity.WARN,
                        subject=spec.table_name,
                        message=f"Projection '{spec.table_name}' last updated {age / 3600:.1f}h ago (max: {spec.max_freshness_seconds / 3600:.1f}h)",
                        evidence="SELECT MAX(updated_at) FROM " + spec.table_name,
                    )
                )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.PROJECTIONS,
        status=status,
        check_count=checks,
        valid_zero=True,
        findings=findings,
        evidence_source="psql SELECT COUNT(*), MAX(updated_at) per table",
    )
