# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContractDriftCompute — cross-repo contract drift classification.

Computes hash-based drift for all contracts against pinned baselines and
classifies field-level changes as BREAKING, ADDITIVE, or NON_BREAKING.
Also validates Kafka boundary parity from kafka_boundaries.yaml.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.

NOTE: This node is not yet implemented (node_not_implemented: true in contract.yaml).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

DriftSeverity = Literal["BREAKING", "ADDITIVE", "NON_BREAKING"]
DriftSensitivity = Literal["STRICT", "STANDARD", "LAX"]
OverallStatus = Literal["clean", "drifted", "breaking"]


class ModelContractFieldChange(BaseModel):
    """A single field-level change within a drifted contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        description="Dotted path to the changed field (e.g. 'input_schema.type')"
    )
    change_type: Literal["modified", "added", "removed"]
    is_breaking: bool
    severity: DriftSeverity


class ModelContractDriftFinding(BaseModel):
    """A drift finding for a single contract file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repository name where the contract was found")
    path: str = Field(description="Path to the contract YAML relative to repo root")
    severity: DriftSeverity
    current_hash: str = Field(description="SHA-256 of the current contract YAML")
    pinned_hash: str = Field(description="SHA-256 from the pinned baseline snapshot")
    field_changes: list[ModelContractFieldChange] = Field(default_factory=list)
    summary: str = Field(description="One-line human-readable drift summary")


class ModelBoundaryFinding(BaseModel):
    """A Kafka boundary staleness finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_name: str = Field(description="Topic name from kafka_boundaries.yaml")
    issue: Literal[
        "producer_file_missing",
        "consumer_file_missing",
        "topic_pattern_mismatch",
        "undeclared_cross_repo_topic",
    ]
    producer_repo: str
    consumer_repo: str
    message: str


class ModelContractDriftComputeRequest(BaseModel):
    """Input for the contract drift compute handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: list[str] = Field(
        default_factory=list,
        description="Repository names to scan. Empty = all 8 canonical repos.",
    )
    baseline_path: str = Field(
        default="",
        description="Path to pinned snapshot directory. Empty = auto-resolve from onex_change_control.",
    )
    dry_run: bool = Field(default=False)
    sensitivity: DriftSensitivity = Field(default="STANDARD")
    severity_threshold: DriftSeverity = Field(default="BREAKING")
    check_boundaries: bool = Field(
        default=True,
        description="When true, validate Kafka boundary parity from kafka_boundaries.yaml.",
    )


class ModelContractDriftComputeResult(BaseModel):
    """Output of the contract drift compute handler."""

    model_config = ConfigDict(extra="forbid")

    drifted_contracts: list[ModelContractDriftFinding] = Field(default_factory=list)
    boundary_findings: list[ModelBoundaryFinding] = Field(default_factory=list)
    staleness_scores: dict[str, float] = Field(
        default_factory=dict,
        description="Per-repo staleness score: 0.0 = clean, 1.0 = fully stale.",
    )
    violations: list[str] = Field(
        default_factory=list,
        description="Flat list of violation summaries for quick triage.",
    )
    overall_status: OverallStatus = Field(default="clean")
    repos_scanned: int = 0
    total_contracts_checked: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeContractDriftCompute:
    """Compute cross-repo contract drift against pinned baselines.

    Pure compute handler — reads contract YAML files and snapshot hashes,
    classifies drift, and validates Kafka boundary parity.

    NOT YET IMPLEMENTED — stub only (node_not_implemented: true).
    """

    def handle(
        self, request: ModelContractDriftComputeRequest
    ) -> ModelContractDriftComputeResult:
        """Classify contract drift across repos against pinned baselines."""
        raise NotImplementedError(  # stub-ok
            "NodeContractDriftCompute is not yet implemented. "
            "See OMN-12222 and contract_sweep SKILL.md drift mode for the algorithm."
        )
