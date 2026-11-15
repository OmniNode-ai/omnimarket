# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment document for the principal grant audit (OMN-21622)."""

from __future__ import annotations

from dataclasses import dataclass


def _nonblank(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive int")


@dataclass(frozen=True, slots=True)
class ModelPrincipalGrantAuditDeployment:
    """Paths, names and the timeout of the audit; all relative to registry_root unless absolute."""

    infra_checkout: str
    script: str
    grants_file: str
    issuer_container: str
    overlay_mount_target: str
    ledger_file: str
    role: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "infra_checkout",
            "script",
            "grants_file",
            "issuer_container",
            "overlay_mount_target",
            "ledger_file",
            "role",
        ):
            _nonblank(getattr(self, name), name)
        _positive_int(self.timeout_seconds, "timeout_seconds")
