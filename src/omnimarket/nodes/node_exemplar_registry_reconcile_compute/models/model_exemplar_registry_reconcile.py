# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared registry state, removal policy and reconciliation plan (OMN-21128)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_exemplar_admission_compute.models.model_exemplar_admission import (
    ModelExemplarAdmissionResult,
    ModelGateReceipt,
    ViolationCode,
)

RegistryLifecycle = Literal["active", "deprecated", "revoked"]
RegistryOperation = Literal["create", "revise", "retain", "deprecate", "revoke"]


class ModelExemplarRegistryRow(BaseModel):
    """One logical source's current version and lifecycle at a commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_source_id: str
    node_id: str
    version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle: RegistryLifecycle
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ModelExemplarRemovalPolicy(BaseModel):
    """Versioned refusal codes requiring revocation rather than deprecation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "exemplar-registry-removal.v1"
    revoke_on_codes: tuple[ViolationCode, ...] = (
        "gate_failed",
        "deny_secret_literal",
        "deny_eval_exec",
        "deny_pickle",
    )


class ModelExemplarRegistryReconcileRequest(BaseModel):
    """Admission evidence and current registry rows supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    admission: ModelExemplarAdmissionResult
    content_sha256: dict[str, str]
    current_rows: list[ModelExemplarRegistryRow] = Field(default_factory=list)
    policy: ModelExemplarRemovalPolicy = Field(
        default_factory=ModelExemplarRemovalPolicy
    )

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        """Require lowercase SHA-256 digests for every supplied content entry."""
        for node_id, digest in value.items():
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError(f"Invalid content_sha256 for {node_id}: {digest}")
        return value


class ModelExemplarRegistryOperation(BaseModel):
    """One deterministic lifecycle operation with its version and evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str
    logical_source_id: str
    node_id: str
    operation: RegistryOperation
    lifecycle_after: RegistryLifecycle
    prior_version_digest: str | None
    version_digest: str | None
    reason_codes: list[str]
    receipt: ModelGateReceipt | None


class ModelExemplarRegistryReconcileResult(BaseModel):
    """Ordered reconciliation plan bound to the repository, commit and policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    commit: str
    policy_version: str
    operations: list[ModelExemplarRegistryOperation]
    counts: dict[str, int]
    current_count: int
    admitted_count: int
    reconciliation_hash: str
