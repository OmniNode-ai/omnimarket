# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodePlatformReadiness — Unified platform readiness gate.

Aggregates verification dimensions into a tri-state report:
- PASS: Dimension healthy, data fresh
- WARN: Dimension degraded or data stale (>24h)
- FAIL: Dimension broken, data missing (>72h), or mock data

ONEX node type: COMPUTE
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_platform_readiness.topics import SOW_PHASE2_REQUIRED_TOPICS

# SSH target for .201 infra checks — override via ONEX_INFRA_SSH_TARGET env var
_INFRA_SSH_TARGET = os.environ.get("ONEX_INFRA_SSH_TARGET", "jonah@192.168.86.201")


class EnumReadinessStatus(StrEnum):
    """Tri-state readiness status."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ModelDimensionResult(BaseModel):
    """Result for a single readiness dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: EnumReadinessStatus
    critical: bool
    freshness: str  # "current", "Xh ago", "stale", "missing"
    details: str


class ModelDimensionInput(BaseModel):
    """Input data for a single readiness dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    critical: bool = False
    healthy: bool | None = None  # None = missing/not measured
    last_checked: datetime | None = None
    details: str = ""
    is_mock: bool = False


class ModelPlatformReadinessRequest(BaseModel):
    """Input for the platform readiness handler.

    When dimensions is empty, the handler auto-collects all 7 system dimensions.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[ModelDimensionInput] = Field(default_factory=list)
    now: datetime | None = None  # Allow injection for testing


class ModelPlatformReadinessResult(BaseModel):
    """Output of the platform readiness handler."""

    model_config = ConfigDict(extra="forbid")

    overall: EnumReadinessStatus = EnumReadinessStatus.PASS
    dimensions: list[ModelDimensionResult] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


_STALE_THRESHOLD = timedelta(hours=24)
_MISSING_THRESHOLD = timedelta(hours=72)

# Legacy aliases for backward compatibility with existing tests
ReadinessStatus = EnumReadinessStatus
DimensionResult = ModelDimensionResult
DimensionInput = ModelDimensionInput
PlatformReadinessRequest = ModelPlatformReadinessRequest
PlatformReadinessResult = ModelPlatformReadinessResult


class NodePlatformReadiness:
    """Aggregate verification dimensions into a readiness report."""

    def handle(
        self, request: ModelPlatformReadinessRequest
    ) -> ModelPlatformReadinessResult:
        """Evaluate all dimensions and produce readiness report.

        If request.dimensions is empty, auto-collects all 7 system dimensions.
        """
        now = request.now or datetime.now(UTC)
        dimensions = request.dimensions or self._collect_dimensions(now)
        results: list[ModelDimensionResult] = []
        blockers: list[str] = []
        degraded: list[str] = []

        for dim in dimensions:
            result = self._evaluate_dimension(dim, now)
            results.append(result)

            if result.status == EnumReadinessStatus.FAIL:
                blockers.append(f"{result.name}: {result.details}")
            elif result.status == EnumReadinessStatus.WARN:
                degraded.append(f"{result.name}: {result.details}")

        # Overall status
        if blockers:
            overall = EnumReadinessStatus.FAIL
        elif degraded:
            overall = EnumReadinessStatus.WARN
        else:
            overall = EnumReadinessStatus.PASS

        return ModelPlatformReadinessResult(
            overall=overall,
            dimensions=results,
            blockers=blockers,
            degraded=degraded,
            timestamp=now,
        )

    def _collect_dimensions(self, now: datetime) -> list[ModelDimensionInput]:
        """Proactively collect all 7 system readiness dimensions."""
        return [
            self._check_plugin_version(now),
            self._check_docker_image_age(now),
            self._check_migration_watermark(now),
            self._check_kafka_topic_coverage(now),
            self._check_pre_commit_installation(now),
            self._check_quality_score_coverage(now),
            self._check_baselines_freshness(now),
        ]

    def _check_plugin_version(self, now: datetime) -> ModelDimensionInput:
        """Check if onex plugin is installed and up to date."""
        try:
            result = subprocess.run(
                ["claude", "plugin", "list"], capture_output=True, text=True, timeout=10
            )
            healthy = "onex@omninode-tools" in result.stdout
            details = (
                "onex@omninode-tools installed"
                if healthy
                else "onex@omninode-tools not found in plugin list"
            )
        except Exception as e:
            healthy = None
            details = f"Unable to check plugin list: {e}"
        return ModelDimensionInput(
            name="plugin_version",
            critical=True,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _check_docker_image_age(self, now: datetime) -> ModelDimensionInput:
        """Check if omninode-runtime Docker image was built today."""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    _INFRA_SSH_TARGET,
                    "docker inspect omninode-runtime --format '{{.Created}}'",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            created_str = result.stdout.strip().strip("'")
            if created_str and result.returncode == 0:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                # Both now (UTC-aware from request) and created (tz-aware from ISO) are
                # already timezone-aware; subtract directly without replace(tzinfo=...).
                age_hours = (now - created.astimezone(UTC)).total_seconds() / 3600
                healthy = age_hours < 48
                details = f"Image created {int(age_hours)}h ago"
                last_checked = now
            else:
                healthy = None
                details = "Could not determine image creation time"
                last_checked = None
        except Exception as e:
            healthy = None
            details = f"Docker check failed: {e}"
            last_checked = None
        return ModelDimensionInput(
            name="docker_image_age",
            critical=True,
            healthy=healthy,
            last_checked=last_checked or now,
            details=details,
        )

    def _check_migration_watermark(self, now: datetime) -> ModelDimensionInput:
        """Check that DB migration watermark is >= 062."""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    _INFRA_SSH_TARGET,
                    "docker exec omnibase-infra-postgres psql -U postgres -d omnibase_infra -t -c "
                    '"SELECT migration_id FROM schema_migrations ORDER BY applied_at DESC LIMIT 1;"',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                healthy = None
                details = f"Migration check failed (exit {result.returncode}): {result.stderr.strip()}"
            else:
                last_migration = result.stdout.strip()
                # Extract numeric part from migration_id like "docker/062_..."
                parts = last_migration.split("/")
                if len(parts) >= 2:
                    num_str = parts[-1][:3]
                    num = int(num_str) if num_str.isdigit() else 0
                    healthy = num >= 62
                    details = (
                        f"Latest migration: {last_migration.strip()} (watermark {num})"
                    )
                else:
                    healthy = False
                    details = f"Could not parse migration watermark: {last_migration}"
        except Exception as e:
            healthy = None
            details = f"Migration check failed: {e}"
        return ModelDimensionInput(
            name="migration_watermark",
            critical=True,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _check_kafka_topic_coverage(self, now: datetime) -> ModelDimensionInput:
        """Check that SOW Phase 2 Kafka topics exist."""
        required_topics = SOW_PHASE2_REQUIRED_TOPICS
        try:
            result = subprocess.run(
                [
                    "ssh",
                    _INFRA_SSH_TARGET,
                    "docker exec omnibase-infra-redpanda rpk topic list 2>/dev/null",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                healthy = None
                details = f"Kafka topic check failed (exit {result.returncode}): {result.stderr.strip()}"
                return ModelDimensionInput(
                    name="kafka_topic_coverage",
                    critical=False,
                    healthy=healthy,
                    last_checked=now,
                    details=details,
                )
            # Parse topic names from rpk output — first token on each line
            existing = {
                line.split()[0]
                for line in result.stdout.splitlines()
                if line.strip() and not line.startswith("NAME")
            }
            missing = [t for t in required_topics if t not in existing]
            healthy = len(missing) == 0
            details = (
                "All SOW Phase 2 topics present"
                if healthy
                else f"Missing topics: {', '.join(missing)}"
            )
        except Exception as e:
            healthy = None
            details = f"Kafka topic check failed: {e}"
        return ModelDimensionInput(
            name="kafka_topic_coverage",
            critical=False,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _check_pre_commit_installation(self, now: datetime) -> ModelDimensionInput:
        """Check pre-commit hooks are installed in key repos."""
        omni_home_env = os.environ.get("OMNI_HOME")
        if not omni_home_env:
            return ModelDimensionInput(
                name="pre_commit_installation",
                critical=False,
                healthy=None,
                last_checked=now,
                details="OMNI_HOME env var not set; pre-commit check skipped",
            )
        omni_home = Path(omni_home_env)
        repos_to_check = ["omniclaude", "omnibase_core", "omnibase_infra", "omnimarket"]
        missing = []
        for repo in repos_to_check:
            hook_path = omni_home / repo / ".git" / "hooks" / "pre-commit"
            if not (hook_path.exists() and os.access(hook_path, os.X_OK)):
                missing.append(repo)
        healthy = len(missing) == 0
        details = (
            f"pre-commit hooks installed in all {len(repos_to_check)} key repos"
            if healthy
            else f"pre-commit not installed in: {', '.join(missing)}"
        )
        return ModelDimensionInput(
            name="pre_commit_installation",
            critical=False,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _check_quality_score_coverage(self, now: datetime) -> ModelDimensionInput:
        """Check routing_outcomes table has non-null quality scores."""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    _INFRA_SSH_TARGET,
                    "docker exec omnibase-infra-postgres psql -U postgres -d omnibase_infra -t -c "
                    '"SELECT COUNT(*) FROM routing_outcomes WHERE quality_score IS NOT NULL;"',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                healthy = None
                details = f"Quality score check failed (exit {result.returncode}): {result.stderr.strip()}"
            else:
                count_str = result.stdout.strip()
                count = int(count_str) if count_str.isdigit() else 0
                healthy = count > 0
                details = f"{count} routing outcomes with quality scores"
        except Exception as e:
            healthy = None
            details = f"Quality score check failed: {e}"
        return ModelDimensionInput(
            name="quality_score_coverage",
            critical=False,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _check_baselines_freshness(self, now: datetime) -> ModelDimensionInput:
        """Check baselines tables have recent data."""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    _INFRA_SSH_TARGET,
                    "docker exec omnibase-infra-postgres psql -U postgres -d omnibase_infra -t -c "
                    "\"SELECT COUNT(*) FROM baselines WHERE created_at > NOW() - INTERVAL '7 days';\"",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                healthy = None
                details = f"Baselines check failed (exit {result.returncode}): {result.stderr.strip()}"
            else:
                count_str = result.stdout.strip()
                count = int(count_str) if count_str.isdigit() else 0
                healthy = count > 0
                details = f"{count} baseline records in last 7 days"
        except Exception as e:
            healthy = None
            details = f"Baselines check failed: {e}"
        return ModelDimensionInput(
            name="baselines_freshness",
            critical=False,
            healthy=healthy,
            last_checked=now,
            details=details,
        )

    def _evaluate_dimension(
        self, dim: ModelDimensionInput, now: datetime
    ) -> ModelDimensionResult:
        """Evaluate a single dimension with freshness rules."""
        # Mock data is always FAIL
        if dim.is_mock:
            return ModelDimensionResult(
                name=dim.name,
                status=EnumReadinessStatus.FAIL,
                critical=dim.critical,
                freshness="mock",
                details=f"Mock data detected: {dim.details}",
            )

        # Missing data
        if dim.healthy is None or dim.last_checked is None:
            return ModelDimensionResult(
                name=dim.name,
                status=EnumReadinessStatus.FAIL,
                critical=dim.critical,
                freshness="missing",
                details=dim.details or "No data available",
            )

        # Freshness check
        age = now - dim.last_checked
        if age > _MISSING_THRESHOLD:
            freshness = f">{int(age.total_seconds() / 3600)}h (missing)"
            return ModelDimensionResult(
                name=dim.name,
                status=EnumReadinessStatus.FAIL,
                critical=dim.critical,
                freshness=freshness,
                details=f"Data too old to trust ({freshness})",
            )

        if age > _STALE_THRESHOLD:
            freshness = f"{int(age.total_seconds() / 3600)}h ago (stale)"
            return ModelDimensionResult(
                name=dim.name,
                status=EnumReadinessStatus.WARN,
                critical=dim.critical,
                freshness=freshness,
                details=dim.details or f"Stale data ({freshness})",
            )

        # Fresh data — use actual status
        hours = int(age.total_seconds() / 3600)
        freshness = "current" if hours == 0 else f"{hours}h ago"
        status = EnumReadinessStatus.PASS if dim.healthy else EnumReadinessStatus.FAIL

        return ModelDimensionResult(
            name=dim.name,
            status=status,
            critical=dim.critical,
            freshness=freshness,
            details=dim.details or ("Healthy" if dim.healthy else "Unhealthy"),
        )
