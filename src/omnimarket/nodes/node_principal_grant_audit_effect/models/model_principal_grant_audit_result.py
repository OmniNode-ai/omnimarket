# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Steps and totals of the principal grant audit handler (OMN-21622)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelPrincipalGrantAuditPrincipal:
    """One principal's expected versus live grant counts."""

    principal: str
    expected: int
    live: int
    missing: int
    extra: int


@dataclass(frozen=True, slots=True)
class ModelPrincipalGrantAuditStep:
    """One subprocess call: its step name, argv, exit code and the last line it printed."""

    step: str
    argv: tuple[str, ...]
    exit_code: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelPrincipalGrantAuditResult:
    """What the audit found. status is ok, drift, audit_error, container_unreadable or dry_run."""

    status: str
    exit_code: int
    principals: tuple[ModelPrincipalGrantAuditPrincipal, ...] = ()
    missing_lines: tuple[str, ...] = ()
    extra_lines: tuple[str, ...] = ()
    steps: tuple[ModelPrincipalGrantAuditStep, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    def line(self) -> str:
        return (
            f"principals={len(self.principals)} "
            f"missing={len(self.missing_lines)} "
            f"extra={len(self.extra_lines)} "
            f"status={self.status}"
        )
