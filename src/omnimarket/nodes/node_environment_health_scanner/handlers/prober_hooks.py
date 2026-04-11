"""Hook error rate prober.

Reads $ONEX_STATE_DIR/hooks/logs/:
- violations.log  — raw violation events (JSON lines or JSON array)
- violations_summary.json — aggregated counts per hook: {"hook_name": {"total": N, "errors": M}}

Flags hooks with >5% error rate as WARN (>15% as FAIL).
"""

from __future__ import annotations

import json
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

_WARN_THRESHOLD = 0.05  # 5%
_FAIL_THRESHOLD = 0.15  # 15%


def probe_hooks(log_dir: str) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []
    log_path = Path(log_dir)

    if not log_path.exists():
        return ModelSubsystemResult(
            subsystem=EnumSubsystem.HOOKS,
            status=EnumReadinessStatus.FAIL,
            check_count=0,
            findings=[
                ModelHealthFinding(
                    subsystem=EnumSubsystem.HOOKS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject="log_dir",
                    message=f"Hook log directory not found: {log_dir}",
                    evidence=f"Path.exists() returned False for {log_dir}",
                )
            ],
            evidence_source=log_dir,
        )

    checks = 0
    # Read violations_summary.json if present
    summary_path = log_path / "violations_summary.json"
    if summary_path.exists():
        try:
            raw = json.loads(summary_path.read_text())
            checks = 1
            if isinstance(raw, dict):
                # Format A: {"hook_name": {"total": N, "errors": M}} — per-hook error rates
                if all(isinstance(v, dict) for v in raw.values()):
                    for hook_name, counts in raw.items():
                        if not isinstance(counts, dict):
                            continue
                        total = int(counts.get("total", 0) or 0)
                        errors = int(counts.get("errors", 0) or 0)
                        if total == 0:
                            continue
                        rate = errors / total
                        if rate > _FAIL_THRESHOLD:
                            findings.append(
                                ModelHealthFinding(
                                    subsystem=EnumSubsystem.HOOKS,
                                    severity=EnumHealthFindingSeverity.FAIL,
                                    subject=hook_name,
                                    message=f"Hook '{hook_name}' error rate {rate:.1%} exceeds {_FAIL_THRESHOLD:.0%} threshold ({errors}/{total})",
                                    evidence=str(summary_path),
                                )
                            )
                        elif rate > _WARN_THRESHOLD:
                            findings.append(
                                ModelHealthFinding(
                                    subsystem=EnumSubsystem.HOOKS,
                                    severity=EnumHealthFindingSeverity.WARN,
                                    subject=hook_name,
                                    message=f"Hook '{hook_name}' error rate {rate:.1%} exceeds {_WARN_THRESHOLD:.0%} threshold ({errors}/{total})",
                                    evidence=str(summary_path),
                                )
                            )
                # Format B: {"last_updated": ..., "total_violations_today": N, "files_with_violations": [...]}
                elif "total_violations_today" in raw:
                    total_violations = int(raw.get("total_violations_today", 0))
                    if total_violations > 0:
                        files_with_violations = raw.get("files_with_violations", [])
                        severity = (
                            EnumHealthFindingSeverity.WARN
                            if total_violations < 20
                            else EnumHealthFindingSeverity.FAIL
                        )
                        findings.append(
                            ModelHealthFinding(
                                subsystem=EnumSubsystem.HOOKS,
                                severity=severity,
                                subject="violations_summary.json",
                                message=f"{total_violations} hook violations today across {len(files_with_violations)} files",
                                evidence=str(summary_path),
                            )
                        )
                else:
                    findings.append(
                        ModelHealthFinding(
                            subsystem=EnumSubsystem.HOOKS,
                            severity=EnumHealthFindingSeverity.WARN,
                            subject="violations_summary.json",
                            message="Unsupported or malformed violations_summary schema",
                            evidence=str(summary_path),
                        )
                    )
        except (json.JSONDecodeError, KeyError, ValueError):
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.HOOKS,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject="violations_summary.json",
                    message="Failed to parse violations_summary.json",
                    evidence=str(summary_path),
                )
            )
    else:
        # Fall back to violations.log — read content to count actual violations
        violations_path = log_path / "violations.log"
        if violations_path.exists():
            checks = 1
            try:
                raw_content = violations_path.read_text()
                # Try parsing as a JSON array first; fall back to JSONL line count
                stripped = raw_content.strip()
                try:
                    parsed = json.loads(stripped)
                    violation_count = len(parsed) if isinstance(parsed, list) else 0
                except json.JSONDecodeError:
                    violation_count = sum(
                        1
                        for ln in raw_content.splitlines()
                        if ln.strip() and ln.strip() not in ("[]", "{}", "")
                    )
                if violation_count:
                    severity = (
                        EnumHealthFindingSeverity.FAIL
                        if violation_count >= 20
                        else EnumHealthFindingSeverity.WARN
                    )
                    findings.append(
                        ModelHealthFinding(
                            subsystem=EnumSubsystem.HOOKS,
                            severity=severity,
                            subject="violations.log",
                            message=f"{violation_count} hook violation(s) recorded",
                            evidence=str(violations_path),
                        )
                    )
            except OSError:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.HOOKS,
                        severity=EnumHealthFindingSeverity.FAIL,
                        subject="violations.log",
                        message="Failed to read violations.log",
                        evidence=str(violations_path),
                    )
                )
        else:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.HOOKS,
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
