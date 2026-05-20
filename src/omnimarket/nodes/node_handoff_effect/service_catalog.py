# SPDX-License-Identifier: MIT
"""Reusable .201 service catalog probe.

Each service is probed independently via SSH. A single probe failure does not
prevent the remaining probes from running. The caller receives a
ServiceCatalogSnapshot listing every service with its raw status, a healthy
flag, and the probe timestamp.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class ServiceProbeResult(BaseModel):
    """Result for a single probed service."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    healthy: bool
    probe_timestamp: datetime
    error: str | None = None


class ServiceCatalogSnapshot(BaseModel):
    """Snapshot of all probed .201 services at a point in time."""

    model_config = ConfigDict(frozen=True)

    snapshot_timestamp: datetime
    services: tuple[ServiceProbeResult, ...]

    def is_fully_healthy(self) -> bool:
        return all(s.healthy for s in self.services)

    def get(self, name: str) -> ServiceProbeResult | None:
        for s in self.services:
            if s.name == name:
                return s
        return None


_SERVICE_PROBES: tuple[tuple[str, str, str, bool], ...] = (
    # (name, remote_cmd, healthy_match, exact_match)
    (
        "postgres",
        "docker ps --filter name=postgres --format '{{.Names}}|{{.Status}}'",
        "Up",
        False,
    ),
    (
        "kafka",
        "docker ps --filter name=redpanda --format '{{.Names}}|{{.Status}}'",
        "Up",
        False,
    ),
    (
        "valkey",
        "docker ps --filter name=valkey --format '{{.Names}}|{{.Status}}'",
        "Up",
        False,
    ),
    (
        "infisical",
        "docker ps --filter name=infisical --format '{{.Names}}|{{.Status}}'",
        "Up",
        False,
    ),
    (
        "omninode-runtime-effects",
        "docker inspect omninode-runtime-effects --format='{{.State.Health.Status}}'",
        "healthy",
        True,
    ),
    (
        "deploy-agent.service",
        "systemctl --user is-active deploy-agent.service",
        "active",
        True,
    ),
)


def _run_ssh_probe(
    ssh_target: str,
    remote_cmd: str,
    timeout_s: int,
) -> tuple[str, str | None]:
    """Run one SSH probe. Returns (stdout, error_message | None)."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                f"ConnectTimeout={timeout_s}",
                "-o",
                "BatchMode=yes",
                ssh_target,
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 2,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            return "", f"exit {result.returncode}: {result.stderr.strip()}"
        return output, None
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout_s}s"
    except OSError as exc:
        return "", str(exc)


def probe_services(
    ssh_target: str,
    timeout_s: int = 5,
    service_probes: Sequence[tuple[str, str, str, bool]] | None = None,
) -> ServiceCatalogSnapshot:
    """Probe all .201 services and return a snapshot.

    Args:
        ssh_target: SSH connection string, e.g. user@host.  # onex-allow-internal-ip: docstring example only, not a runtime default
        timeout_s: Per-probe SSH timeout in seconds. Default 5.
        service_probes: Override the default probe list. Each element is
            (name, remote_cmd, healthy_match, exact_match). When exact_match
            is True the full stdout must equal healthy_match; otherwise
            healthy_match must be a substring of stdout.

    Returns:
        ServiceCatalogSnapshot with one ServiceProbeResult per service.
    """
    probes: Sequence[tuple[str, str, str, bool]] = (
        service_probes if service_probes is not None else _SERVICE_PROBES
    )
    snapshot_ts = datetime.now(UTC)
    results: list[ServiceProbeResult] = []

    for name, remote_cmd, healthy_match, exact_match in probes:
        probe_ts = datetime.now(UTC)
        output, error = _run_ssh_probe(ssh_target, remote_cmd, timeout_s)
        if error:
            healthy = False
        elif exact_match:
            healthy = output == healthy_match
        else:
            healthy = healthy_match in output
        results.append(
            ServiceProbeResult(
                name=name,
                status=output or (error or ""),
                healthy=healthy,
                probe_timestamp=probe_ts,
                error=error,
            )
        )
        logger.debug(
            "probe %s: status=%r healthy=%s error=%s", name, output, healthy, error
        )

    return ServiceCatalogSnapshot(
        snapshot_timestamp=snapshot_ts,
        services=tuple(results),
    )


__all__ = [
    "ServiceCatalogSnapshot",
    "ServiceProbeResult",
    "probe_services",
]
