"""Emit daemon health prober.

Checks:
1. Unix socket exists at ONEX_EMIT_DAEMON_SOCKET (default: /tmp/onex_emit.sock)
2. Log directory contains recent emit activity
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

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

_DEFAULT_SOCKET = os.environ.get("ONEX_EMIT_DAEMON_SOCKET", "/tmp/onex_emit.sock")
_STALE_THRESHOLD_SECONDS = 3600  # last publish >1h ago = WARN


def probe_emit_daemon(
    socket_path: str = _DEFAULT_SOCKET,
    log_dir: str | None = None,
    ssh_target: str | None = None,
) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []

    # Check 1: socket exists as a socket (not a directory or regular file)
    socket_p = Path(socket_path)
    if not socket_p.exists() or not socket_p.is_socket():
        findings.append(
            ModelHealthFinding(
                subsystem=EnumSubsystem.EMIT_DAEMON,
                severity=EnumHealthFindingSeverity.FAIL,
                subject="socket",
                message=f"Emit daemon socket not found: {socket_path}",
                evidence=f"Path.is_socket() returned False for {socket_path}",
            )
        )

    # Check 2: log directory has recent activity
    if log_dir:
        log_path = Path(log_dir)
        emit_logs = (
            sorted(
                log_path.glob("emit*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if log_path.exists()
            else []
        )
        if not emit_logs:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.EMIT_DAEMON,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject="logs",
                    message=f"No emit logs found in {log_dir}",
                    evidence=f"glob('emit*.log') returned empty in {log_dir}",
                )
            )
        else:
            latest_mtime = emit_logs[0].stat().st_mtime
            age = datetime.now(UTC).timestamp() - latest_mtime
            if age > _STALE_THRESHOLD_SECONDS:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.EMIT_DAEMON,
                        severity=EnumHealthFindingSeverity.WARN,
                        subject="logs",
                        message=f"Emit log last modified {age / 3600:.1f}h ago (threshold: {_STALE_THRESHOLD_SECONDS / 3600:.0f}h)",
                        evidence=str(emit_logs[0]),
                    )
                )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.EMIT_DAEMON,
        status=status,
        check_count=2 if log_dir else 1,
        findings=findings,
        evidence_source=socket_path,
    )
