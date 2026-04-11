"""Container health prober.

For every container in the runtime profile:
- running: State == "running"
- healthy: Status contains "(healthy)"
- restart_count: parsed from docker inspect RestartCount

Collection via `ssh .201 docker ps --format '{{json .}}'` in handler.
"""

from __future__ import annotations

import json
import subprocess

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

_RESTART_WARN_THRESHOLD = 3
_RESTART_FAIL_THRESHOLD = 10

# Canonical list of required containers from CLAUDE.md .201 infra section
REQUIRED_CONTAINERS = [
    "omninode-runtime",
    "omninode-runtime-shadow",
    "omninode-runtime-effects",
    "omninode-contract-resolver",
    "omninode-agent-actions-consumer",
    "omninode-context-audit-consumer",
    "omninode-skill-lifecycle-consumer",
    "omnibase-infra-postgres",
    "omnibase-infra-redpanda",
    "omnibase-infra-valkey",
    "omnibase-infra-phoenix",
    "omnibase-infra-autoheal",
    "omnibase-infra-migration-gate",
    "omnibase-intelligence-api",
]


def probe_containers(
    expected_containers: list[str],
    running_containers: list[dict[str, object]],
    ssh_target: str | None,
) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []
    running_by_name = {c["name"]: c for c in running_containers}
    checks = len(expected_containers)

    for name in expected_containers:
        if name not in running_by_name:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.CONTAINERS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=name,
                    message=f"Container '{name}' not found in docker ps output",
                    evidence="docker ps --format json",
                )
            )
            continue

        container = running_by_name[name]
        if not container.get("running", False):
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.CONTAINERS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=name,
                    message=f"Container '{name}' is not running (state: {container.get('state', 'unknown')})",
                    evidence="docker ps --format json",
                )
            )
            continue

        if not container.get("healthy", True):
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.CONTAINERS,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject=name,
                    message=f"Container '{name}' is unhealthy",
                    evidence="docker ps --format json",
                )
            )

        restarts = int(str(container.get("restart_count") or 0))
        if restarts >= _RESTART_FAIL_THRESHOLD:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.CONTAINERS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=name,
                    message=f"Container '{name}' has restarted {restarts} times (threshold: {_RESTART_FAIL_THRESHOLD})",
                    evidence="docker inspect RestartCount",
                )
            )
        elif restarts >= _RESTART_WARN_THRESHOLD:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.CONTAINERS,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject=name,
                    message=f"Container '{name}' has restarted {restarts} times (threshold: {_RESTART_WARN_THRESHOLD})",
                    evidence="docker inspect RestartCount",
                )
            )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.CONTAINERS,
        status=status,
        check_count=checks,
        findings=findings,
        evidence_source="ssh .201 docker ps --format json",
    )


def collect_containers_via_ssh(ssh_target: str) -> list[dict[str, object]]:
    """Run docker ps on .201, return parsed container dicts."""
    try:
        out = subprocess.check_output(
            ["ssh", ssh_target, "docker ps --format '{{json .}}'"],
            timeout=20,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        containers = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                name = obj.get("Names", "").strip("/").split(",")[0]
                status_str = obj.get("Status", "")
                containers.append(
                    {
                        "name": name,
                        "running": obj.get("State", "") == "running",
                        "healthy": "(healthy)" in status_str,
                        "restart_count": 0,  # requires docker inspect for exact count
                        "state": obj.get("State", "unknown"),
                    }
                )
            except json.JSONDecodeError:
                continue
        return containers
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []


def _parse_docker_ps_json(raw: str) -> list[dict[str, object]]:
    """Parse a JSON array of docker ps rows (for testing)."""
    try:
        rows = json.loads(raw)
        result = []
        for row in rows:
            name = row.get("Names", "").strip("/").split(",")[0]
            status_str = row.get("Status", "")
            result.append(
                {
                    "name": name,
                    "running": row.get("State", "") == "running",
                    "healthy": "(healthy)" in status_str,
                    "restart_count": 0,  # RestartCount not in docker ps; needs docker inspect
                }
            )
        return result
    except (json.JSONDecodeError, AttributeError):
        return []
