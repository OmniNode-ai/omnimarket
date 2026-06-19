"""Hook error rate prober.

Reads $ONEX_STATE_DIR/hooks/logs/:
- violations.log  — raw violation events (JSON lines or JSON array)
- violations_summary.json — aggregated counts per hook: {"hook_name": {"total": N, "errors": M}}

Flags hooks with >5% error rate as WARN (>15% as FAIL).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

_WARN_THRESHOLD = 0.05  # 5%
_FAIL_THRESHOLD = 0.15  # 15%


def _hook_finding(
    *,
    severity: EnumHealthFindingSeverity,
    subject: str,
    message: str,
    evidence: str,
) -> ModelHealthFinding:
    return ModelHealthFinding(
        subsystem=EnumSubsystem.HOOKS,
        severity=severity,
        subject=subject,
        message=message,
        evidence=evidence,
    )


def _missing_log_dir_result(log_dir: str) -> ModelSubsystemResult:
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.HOOKS,
        status=EnumReadinessStatus.FAIL,
        check_count=0,
        findings=[
            _hook_finding(
                severity=EnumHealthFindingSeverity.FAIL,
                subject="log_dir",
                message=f"Hook log directory not found: {log_dir}",
                evidence=f"Path.exists() returned False for {log_dir}",
            )
        ],
        evidence_source=log_dir,
    )


def _classify_hook_error_rate(
    *, hook_name: str, total: int, errors: int, evidence: str
) -> ModelHealthFinding | None:
    if total == 0:
        return None

    rate = errors / total
    if rate > _FAIL_THRESHOLD:
        severity = EnumHealthFindingSeverity.FAIL
        threshold = _FAIL_THRESHOLD
    elif rate > _WARN_THRESHOLD:
        severity = EnumHealthFindingSeverity.WARN
        threshold = _WARN_THRESHOLD
    else:
        return None

    return _hook_finding(
        severity=severity,
        subject=hook_name,
        message=(
            f"Hook '{hook_name}' error rate {rate:.1%} exceeds "
            f"{threshold:.0%} threshold ({errors}/{total})"
        ),
        evidence=evidence,
    )


def _findings_from_summary_counts(
    raw: dict[str, Any], summary_path: Path
) -> list[ModelHealthFinding]:
    findings: list[ModelHealthFinding] = []
    for hook_name, counts in raw.items():
        if not isinstance(counts, dict):
            continue
        total = int(counts.get("total", 0) or 0)
        errors = int(counts.get("errors", 0) or 0)
        finding = _classify_hook_error_rate(
            hook_name=hook_name,
            total=total,
            errors=errors,
            evidence=str(summary_path),
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _findings_from_daily_summary(
    raw: dict[str, Any], summary_path: Path
) -> list[ModelHealthFinding]:
    total_violations = int(raw.get("total_violations_today", 0))
    if total_violations <= 0:
        return []

    files_with_violations = raw.get("files_with_violations", [])
    severity = (
        EnumHealthFindingSeverity.WARN
        if total_violations < 20
        else EnumHealthFindingSeverity.FAIL
    )
    return [
        _hook_finding(
            severity=severity,
            subject="violations_summary.json",
            message=(
                f"{total_violations} hook violations today across "
                f"{len(files_with_violations)} files"
            ),
            evidence=str(summary_path),
        )
    ]


def _findings_from_summary(raw: object, summary_path: Path) -> list[ModelHealthFinding]:
    if not isinstance(raw, dict):
        return []

    if all(isinstance(v, dict) for v in raw.values()):
        return _findings_from_summary_counts(raw, summary_path)

    if "total_violations_today" in raw:
        return _findings_from_daily_summary(raw, summary_path)

    return [
        _hook_finding(
            severity=EnumHealthFindingSeverity.WARN,
            subject="violations_summary.json",
            message="Unsupported or malformed violations_summary schema",
            evidence=str(summary_path),
        )
    ]


def _read_summary_findings(summary_path: Path) -> list[ModelHealthFinding]:
    try:
        return _findings_from_summary(
            json.loads(summary_path.read_text()), summary_path
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return [
            _hook_finding(
                severity=EnumHealthFindingSeverity.WARN,
                subject="violations_summary.json",
                message="Failed to parse violations_summary.json",
                evidence=str(summary_path),
            )
        ]


def _count_violations(raw_content: str) -> int:
    stripped = raw_content.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return sum(
            1
            for line in raw_content.splitlines()
            if line.strip() and line.strip() not in ("[]", "{}", "")
        )
    return len(parsed) if isinstance(parsed, list) else 0


def _findings_from_violations_log(
    violations_path: Path,
) -> list[ModelHealthFinding]:
    try:
        violation_count = _count_violations(violations_path.read_text())
    except OSError:
        return [
            _hook_finding(
                severity=EnumHealthFindingSeverity.FAIL,
                subject="violations.log",
                message="Failed to read violations.log",
                evidence=str(violations_path),
            )
        ]

    if not violation_count:
        return []

    severity = (
        EnumHealthFindingSeverity.FAIL
        if violation_count >= 20
        else EnumHealthFindingSeverity.WARN
    )
    return [
        _hook_finding(
            severity=severity,
            subject="violations.log",
            message=f"{violation_count} hook violation(s) recorded",
            evidence=str(violations_path),
        )
    ]


def probe_hooks(log_dir: str) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []
    log_path = Path(log_dir)

    if not log_path.exists():
        return _missing_log_dir_result(log_dir)

    checks = 0
    summary_path = log_path / "violations_summary.json"
    if summary_path.exists():
        checks = 1
        findings.extend(_read_summary_findings(summary_path))
    else:
        violations_path = log_path / "violations.log"
        if violations_path.exists():
            checks = 1
            findings.extend(_findings_from_violations_log(violations_path))
        else:
            findings.append(
                _hook_finding(
                    severity=EnumHealthFindingSeverity.WARN,
                    subject="logs",
                    message="No violations_summary.json or violations.log found",
                    evidence=log_dir,
                )
            )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.HOOKS,
        status=status,
        check_count=checks,
        valid_zero=True,
        findings=findings,
        evidence_source=log_dir,
    )
