# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared registry state, removal policy and reconciliation plan (OMN-21128)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.exemplar_admission import (
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

    policy_version: str = "exemplar-registry-removal.v2"
    minimum_sample_size: int = Field(default=3, ge=2, strict=True)
    minimum_precision: float = Field(default=0.8, ge=0.0, le=1.0)
    minimum_recall: float = Field(default=0.8, ge=0.0, le=1.0)
    revoke_on_codes: tuple[ViolationCode, ...] = (
        "gate_failed",
        "deny_secret_literal",
        "deny_eval_exec",
        "deny_pickle",
    )


class ModelExemplarEvaluationCounts(BaseModel):
    """Observed confusion counts; undefined ratios remain unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    true_positive: int = Field(default=0, ge=0, strict=True)
    false_positive: int = Field(default=0, ge=0, strict=True)
    true_negative: int = Field(default=0, ge=0, strict=True)
    false_negative: int = Field(default=0, ge=0, strict=True)

    @property
    def sample_size(self) -> int:
        """Every observed labelled outcome contributes one sample."""
        return (
            self.true_positive
            + self.false_positive
            + self.true_negative
            + self.false_negative
        )

    @property
    def precision(self) -> float | None:
        """Share of predicted positives that were correct."""
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        """Share of actual positives recovered by the exemplar."""
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else None


class ModelExemplarRegistryReconcileRequest(BaseModel):
    """Admission evidence and current registry rows supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    admission: ModelExemplarAdmissionResult
    content_sha256: dict[str, str]
    current_rows: list[ModelExemplarRegistryRow] = Field(default_factory=list)
    evaluation_counts: dict[str, ModelExemplarEvaluationCounts] = Field(
        default_factory=dict
    )
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
    evaluation: ModelExemplarEvaluationCounts = Field(
        default_factory=ModelExemplarEvaluationCounts
    )


class ModelExemplarRegistryReconcileResult(BaseModel):
    """Ordered reconciliation plan bound to the repository, commit and policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    commit: str
    policy_version: str
    policy: ModelExemplarRemovalPolicy = Field(
        default_factory=ModelExemplarRemovalPolicy
    )
    operations: list[ModelExemplarRegistryOperation]
    counts: dict[str, int]
    current_count: int
    admitted_count: int
    reconciliation_hash: str
