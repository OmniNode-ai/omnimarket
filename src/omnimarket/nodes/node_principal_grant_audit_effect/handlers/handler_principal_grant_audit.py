# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the machine principal grant audit script and parse its output (OMN-21622).

The script's logic is never reimplemented: this handler resolves the issuer's overlay
file on the host, runs the audit script with the overlay's paths and role, and parses
what it prints into a typed result. No secret is read or printed; nothing is written.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..models.model_principal_grant_audit_deployment import (
    ModelPrincipalGrantAuditDeployment,
)
from ..models.model_principal_grant_audit_request import ModelPrincipalGrantAuditRequest
from ..models.model_principal_grant_audit_result import (
    ModelPrincipalGrantAuditPrincipal,
    ModelPrincipalGrantAuditResult,
    ModelPrincipalGrantAuditStep,
)

OVERLAY_ENV = "OMNIMARKET_PRINCIPAL_GRANT_AUDIT_OVERLAY"
PRINCIPAL_RE = re.compile(
    r"^principal=(\S+) expected=(\d+) live=(\d+) missing=(\d+) extra=(\d+)$"
)


class PrincipalGrantAuditConfigurationError(ValueError):
    """Missing or invalid deployment configuration."""


def load_deployment_overlay() -> ModelPrincipalGrantAuditDeployment | None:
    """Read the explicit pointer, otherwise the first existing overlay in root order."""
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise PrincipalGrantAuditConfigurationError(
                f"{OVERLAY_ENV}: empty overlay pointer"
            )
        path = Path(pointer)
        source = OVERLAY_ENV
    else:
        path = None
        source = "ONEX_SKILL_OVERLAY_ROOTS"
        for root in os.environ.get(source, "").split(os.pathsep):
            if not root:
                continue
            candidate = (
                Path(root) / "node_principal_grant_audit_effect" / "overlay.yaml"
            )
            if candidate.exists():
                path = candidate
                break
    if path is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        expected = {
            "infra_checkout",
            "script",
            "grants_file",
            "issuer_container",
            "overlay_mount_target",
            "ledger_file",
            "role",
            "timeout_seconds",
        }
        if set(raw) != expected:
            raise ValueError(f"overlay must contain exactly {sorted(expected)}")
        return ModelPrincipalGrantAuditDeployment(
            infra_checkout=raw["infra_checkout"],
            script=raw["script"],
            grants_file=raw["grants_file"],
            issuer_container=raw["issuer_container"],
            overlay_mount_target=raw["overlay_mount_target"],
            ledger_file=raw["ledger_file"],
            role=raw["role"],
            timeout_seconds=raw["timeout_seconds"],
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise PrincipalGrantAuditConfigurationError(
            f"{source}: invalid deployment overlay ({type(exc).__name__})"
        ) from None


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _subprocess_run(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    timeout = int(kw.get("timeout", 300))
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(list(argv), 124, "", f"timed out: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


class HandlerPrincipalGrantAudit:
    """Run the grant audit script and report what it found."""

    def __init__(
        self,
        overlay: ModelPrincipalGrantAuditDeployment | None = None,
        run: Runner | None = None,
        python: str | None = None,
    ) -> None:
        self._overlay = overlay
        self._run: Runner = run or _subprocess_run
        self._python = python or sys.executable

    def handle(
        self, request: ModelPrincipalGrantAuditRequest
    ) -> ModelPrincipalGrantAuditResult:
        deployment = self._overlay or load_deployment_overlay()
        if deployment is None:
            raise PrincipalGrantAuditConfigurationError("overlay not configured")
        steps: list[ModelPrincipalGrantAuditStep] = []
        errors: list[str] = []
        inspect_argv = [
            "docker",
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Destination "'
            + deployment.overlay_mount_target
            + '"}}{{.Source}}{{end}}{{end}}',
            deployment.issuer_container,
        ]
        inspect = self._run(inspect_argv)
        steps.append(
            ModelPrincipalGrantAuditStep(
                step="inspect",
                argv=tuple(inspect_argv),
                exit_code=inspect.returncode,
                detail=_tail(inspect.stdout or inspect.stderr),
            )
        )
        source = inspect.stdout.strip()
        if inspect.returncode != 0 or not source:
            errors.append(f"issuer container {deployment.issuer_container} unreadable")
            return ModelPrincipalGrantAuditResult(
                status="container_unreadable",
                exit_code=2,
                steps=tuple(steps),
                errors=tuple(errors),
            )
        ledger = _resolve(request.registry_root, deployment.ledger_file)
        audit_argv = [
            self._python,
            str(
                _resolve(request.registry_root, deployment.infra_checkout)
                / deployment.script
            ),
            "audit-grants",
            "--grants",
            str(
                _resolve(request.registry_root, deployment.infra_checkout)
                / deployment.grants_file
            ),
            "--ledger",
            str(ledger),
            "--overlay",
            source,
            "--role",
            deployment.role,
            "--rpk-container",
            deployment.issuer_container,
        ]
        if request.dry_run:
            steps.append(
                ModelPrincipalGrantAuditStep(
                    step="audit",
                    argv=tuple(audit_argv),
                    exit_code=0,
                    detail="dry-run",
                )
            )
            return ModelPrincipalGrantAuditResult(
                status="dry_run",
                exit_code=0,
                steps=tuple(steps),
            )
        audit = self._run(audit_argv, timeout=deployment.timeout_seconds)
        steps.append(
            ModelPrincipalGrantAuditStep(
                step="audit",
                argv=tuple(audit_argv),
                exit_code=audit.returncode,
                detail=_tail(audit.stdout or audit.stderr),
            )
        )
        principals: list[ModelPrincipalGrantAuditPrincipal] = []
        missing_lines: list[str] = []
        extra_lines: list[str] = []
        for line in audit.stdout.splitlines():
            match = PRINCIPAL_RE.match(line)
            if match:
                principals.append(
                    ModelPrincipalGrantAuditPrincipal(
                        principal=match.group(1),
                        expected=int(match.group(2)),
                        live=int(match.group(3)),
                        missing=int(match.group(4)),
                        extra=int(match.group(5)),
                    )
                )
            elif line.startswith("MISSING "):
                missing_lines.append(line)
            elif line.startswith("EXTRA "):
                extra_lines.append(line)
        if audit.returncode == 0:
            if not principals:
                errors.append("audit exit 0 with no principal lines: not an audit")
                return ModelPrincipalGrantAuditResult(
                    status="audit_error",
                    exit_code=2,
                    principals=tuple(principals),
                    missing_lines=tuple(missing_lines),
                    extra_lines=tuple(extra_lines),
                    steps=tuple(steps),
                    errors=tuple(errors),
                )
            return ModelPrincipalGrantAuditResult(
                status="ok",
                exit_code=0,
                principals=tuple(principals),
                missing_lines=tuple(missing_lines),
                extra_lines=tuple(extra_lines),
                steps=tuple(steps),
            )
        if audit.returncode == 1:
            return ModelPrincipalGrantAuditResult(
                status="drift",
                exit_code=1,
                principals=tuple(principals),
                missing_lines=tuple(missing_lines),
                extra_lines=tuple(extra_lines),
                steps=tuple(steps),
            )
        errors.append(f"audit exit {audit.returncode}: {audit.stderr[:300]}")
        return ModelPrincipalGrantAuditResult(
            status="audit_error",
            exit_code=2,
            principals=tuple(principals),
            missing_lines=tuple(missing_lines),
            extra_lines=tuple(extra_lines),
            steps=tuple(steps),
            errors=tuple(errors),
        )
