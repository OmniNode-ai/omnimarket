"""NodeEnvironmentHealthScanner — Live environment health scanner.

Reads contracts to know WHAT should exist; probes the environment to verify IF it does.

ONEX node type: COMPUTE (impure — makes live probes)
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_environment_health_scanner.handlers.prober_projections import (
        ModelProjectionSpec,
    )

from datetime import UTC

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


class EnumSubsystem(StrEnum):
    EMIT_DAEMON = "emit_daemon"
    HOOKS = "hooks"
    KAFKA = "kafka"
    CONTAINERS = "containers"
    PROJECTIONS = "projections"
    ENTRY_POINTS = "entry_points"
    MODEL_ENDPOINTS = "model_endpoints"


class EnumHealthFindingSeverity(StrEnum):
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"


class ModelHealthFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subsystem: EnumSubsystem
    severity: EnumHealthFindingSeverity
    subject: str
    message: str
    evidence: str = ""


class ModelSubsystemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subsystem: EnumSubsystem
    status: EnumReadinessStatus
    check_count: int = Field(ge=0, default=0)
    valid_zero: bool = False
    findings: list[ModelHealthFinding] = Field(default_factory=list)
    evidence_source: str = ""
    raw_detail: str = ""


class EnvironmentHealthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subsystems: list[str] = Field(default_factory=list)
    omni_home: str = ""
    ssh_target: str | None = None
    now_override: str | None = None  # ISO datetime string for test injection


class EnvironmentHealthResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall: EnumReadinessStatus = EnumReadinessStatus.PASS
    subsystem_results: list[ModelSubsystemResult] = Field(default_factory=list)
    findings: list[ModelHealthFinding] = Field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(
            1 for r in self.subsystem_results if r.status == EnumReadinessStatus.PASS
        )

    @property
    def warn_count(self) -> int:
        return sum(
            1 for r in self.subsystem_results if r.status == EnumReadinessStatus.WARN
        )

    @property
    def fail_count(self) -> int:
        return sum(
            1 for r in self.subsystem_results if r.status == EnumReadinessStatus.FAIL
        )


def aggregate_status(findings: list[ModelHealthFinding]) -> EnumReadinessStatus:
    """Shared helper used by all probers. Defined here so probers import from the handler module."""
    if any(f.severity == EnumHealthFindingSeverity.FAIL for f in findings):
        return EnumReadinessStatus.FAIL
    if any(f.severity == EnumHealthFindingSeverity.WARN for f in findings):
        return EnumReadinessStatus.WARN
    return EnumReadinessStatus.PASS


class NodeEnvironmentHealthScanner:
    """Live environment health scanner — aggregates 7 subsystem probers."""

    def handle(self, request: EnvironmentHealthRequest) -> EnvironmentHealthResult:
        """Run requested subsystem probers and aggregate results."""
        subsystems_to_run = (
            set(request.subsystems)
            if request.subsystems
            else {s.value for s in EnumSubsystem}
        )
        omni_home = request.omni_home or os.environ.get("OMNI_HOME", "")
        ssh_target = request.ssh_target or os.environ.get("ONEX_INFRA_SSH_TARGET")
        onex_state_dir = os.environ.get(
            "ONEX_STATE_DIR", str(Path.home() / ".onex_state")
        )

        results: list[ModelSubsystemResult] = []

        if EnumSubsystem.EMIT_DAEMON.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_emit_daemon import (
                probe_emit_daemon,
            )

            socket_path = os.environ.get(
                "ONEX_EMIT_DAEMON_SOCKET", "/tmp/onex_emit.sock"
            )
            log_dir = str(Path(onex_state_dir) / "emit")
            results.append(
                probe_emit_daemon(
                    socket_path=socket_path, log_dir=log_dir, ssh_target=ssh_target
                )
            )

        if EnumSubsystem.HOOKS.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_hooks import (
                probe_hooks,
            )

            log_dir = str(Path(onex_state_dir) / "hooks" / "logs")
            results.append(probe_hooks(log_dir=log_dir))

        if EnumSubsystem.KAFKA.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_kafka import (
                collect_kafka_groups_via_ssh,
                collect_kafka_topics_via_ssh,
                probe_kafka,
            )

            declared = self._collect_declared_topics(omni_home)
            existing = collect_kafka_topics_via_ssh(ssh_target) if ssh_target else []
            groups = collect_kafka_groups_via_ssh(ssh_target) if ssh_target else []
            results.append(
                probe_kafka(
                    declared_topics=declared,
                    existing_topics=existing,
                    consumer_groups=groups,
                    ssh_target=ssh_target,
                )
            )

        if EnumSubsystem.CONTAINERS.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_containers import (
                REQUIRED_CONTAINERS,
                collect_containers_via_ssh,
                probe_containers,
            )

            running = collect_containers_via_ssh(ssh_target) if ssh_target else []
            results.append(
                probe_containers(
                    expected_containers=REQUIRED_CONTAINERS,
                    running_containers=running,
                    ssh_target=ssh_target,
                )
            )

        if EnumSubsystem.PROJECTIONS.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_projections import (
                probe_projections,
            )

            specs = self._collect_projection_specs(ssh_target)
            results.append(probe_projections(specs=specs))

        if EnumSubsystem.ENTRY_POINTS.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_entry_points import (
                probe_entry_points,
            )

            results.append(probe_entry_points())

        if EnumSubsystem.MODEL_ENDPOINTS.value in subsystems_to_run:
            from omnimarket.nodes.node_environment_health_scanner.handlers.prober_model_endpoints import (
                probe_model_endpoints,
            )

            results.append(probe_model_endpoints())

        all_findings = [f for r in results for f in r.findings]
        overall = self._aggregate_overall(results)
        return EnvironmentHealthResult(
            overall=overall,
            subsystem_results=results,
            findings=all_findings,
        )

    def _aggregate_overall(
        self, results: list[ModelSubsystemResult]
    ) -> EnumReadinessStatus:
        if any(r.status == EnumReadinessStatus.FAIL for r in results):
            return EnumReadinessStatus.FAIL
        if any(r.status == EnumReadinessStatus.WARN for r in results):
            return EnumReadinessStatus.WARN
        return EnumReadinessStatus.PASS

    def _collect_declared_topics(self, omni_home: str) -> list[str]:
        """Walk omni_home contract.yaml files and collect all declared topics."""
        if not omni_home:
            return []
        import yaml

        topics: set[str] = set()
        for contract_path in Path(omni_home).rglob("contract.yaml"):
            if "nodes" not in str(contract_path):
                continue
            try:
                raw = yaml.safe_load(contract_path.read_text())
                if not isinstance(raw, dict):
                    continue
                bus = raw.get("event_bus", {}) or {}
                for t in bus.get("subscribe_topics", []) or []:
                    if isinstance(t, str):
                        topics.add(t)
                for t in bus.get("publish_topics", []) or []:
                    if isinstance(t, str):
                        topics.add(t)
            except Exception:
                continue
        return sorted(topics)

    def _collect_projection_specs(
        self, ssh_target: str | None
    ) -> list[ModelProjectionSpec]:
        """Query projection table row counts and timestamps via SSH psql."""
        import subprocess

        from omnimarket.nodes.node_environment_health_scanner.handlers.prober_projections import (
            ModelProjectionSpec,
        )

        # Projection tables with freshness SLOs — verified against migrations:
        # - registration_projections: 001_registration_projection.sql:51, updated_at:83
        # - llm_cost_aggregates: 031_create_llm_call_metrics_and_cost_aggregates.sql:137, updated_at:155
        # - baselines_comparisons: 050_create_baselines_tables.sql:43, updated_at:74
        # - baselines_breakdown: 050_create_baselines_tables.sql:144, updated_at:166
        table_slos: dict[str, int] = {
            "registration_projections": 3600,
            "llm_cost_aggregates": 3600,
            "baselines_comparisons": 86400,
            "baselines_breakdown": 86400,
        }

        if not ssh_target:
            return [
                ModelProjectionSpec(
                    table_name=table, max_freshness_seconds=max_freshness
                )
                for table, max_freshness in table_slos.items()
            ]

        specs = []
        for table, max_freshness in table_slos.items():
            try:
                query = f"SELECT COUNT(*), MAX(updated_at) FROM {table};"
                cmd = [
                    "ssh",
                    ssh_target,
                    f'docker exec omnibase-infra-postgres psql -U onex -d onex -t -c "{query}"',
                ]
                out = subprocess.check_output(
                    cmd, timeout=15, text=True, stderr=subprocess.DEVNULL
                )
                parts = out.strip().split("|")
                row_count = int(parts[0].strip()) if parts else 0
                last_updated_str = parts[1].strip() if len(parts) > 1 else None
                last_updated = None
                if last_updated_str and last_updated_str not in ("", "null", "NULL"):
                    from datetime import datetime

                    parsed = datetime.fromisoformat(last_updated_str.replace(" ", "T"))
                    # Ensure timezone-aware to avoid TypeError when comparing with UTC datetimes
                    last_updated = (
                        parsed
                        if parsed.tzinfo is not None
                        else parsed.replace(tzinfo=UTC)
                    )
                specs.append(
                    ModelProjectionSpec(
                        table_name=table,
                        max_freshness_seconds=max_freshness,
                        row_count=row_count,
                        last_updated=last_updated,
                    )
                )
            except Exception:
                specs.append(
                    ModelProjectionSpec(
                        table_name=table,
                        max_freshness_seconds=max_freshness,
                        row_count=0,
                        last_updated=None,
                    )
                )
        return specs
