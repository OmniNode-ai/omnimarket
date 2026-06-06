# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_pipeline_audit_orchestrator [OMN-12211].

ModelPipelineAuditResult: aggregated audit outcome emitted after all phases
complete.

Supporting models:
- EnumFindingSeverity: five-tier severity for gap register entries
- EnumProofCategory: six proof categories per the pipeline_audit skill spec
- EnumFindingStatus: verdict for a single proof item
- ModelRepoInventory: per-repo capability inventory (Phase 2 output)
- ModelGapFinding: single entry in the severity-ordered gap register (Phase 5)
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPipelineAuditStatus(StrEnum):
    """Overall run status for the pipeline audit orchestration."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    ABORTED = "aborted"


class EnumFindingSeverity(StrEnum):
    """Severity tier for gap register findings (ordered highest to lowest)."""

    BREAKING = "breaking"
    """Messages will be rejected at runtime (type mismatch, extra="forbid" violation,
    topic mismatch, missing required field)."""

    CRITICAL = "critical"
    """Data corruption or silent data loss (DSN mismatch, schema column type mismatch)."""

    HIGH = "high"
    """Missing wiring — feature will not work (stub entrypoint, missing consumer,
    .env variable missing)."""

    MEDIUM = "medium"
    """Schema drift — will cause problems eventually (extra columns, inconsistent naming,
    optional-should-be-required fields)."""

    LOW = "low"
    """Tracing and observability gaps (correlation ID break, missing logging,
    no deserialization error handling)."""


class EnumProofCategory(StrEnum):
    """Six proof categories per the pipeline_audit skill specification."""

    ENTRYPOINT = "entrypoint"
    """Runtime entrypoint proof — every repo reaches REAL/STUB/MISSING status."""

    DSN = "dsn"
    """DSN proof — all repos connect to the same database."""

    WIRE_TOPICS = "wire_topics"
    """Wire topics table — byte-for-byte producer/consumer topic match."""

    SCHEMA_HANDSHAKE = "schema_handshake"
    """Schema handshake — shared table columns compared across writer/reader/dashboard."""

    WIRE_FORMAT = "wire_format"
    """Wire format compatibility — Pydantic message models compared field-by-field."""

    CORRELATION = "correlation"
    """Correlation ID threading — tracing identifier traced through every stage."""


class EnumFindingStatus(StrEnum):
    """Verdict for a single proof item."""

    PROVEN = "proven"
    ASSUMED = "assumed"
    GAP = "gap"
    ALIGNED = "aligned"
    COMPATIBLE = "compatible"
    THREADED = "threaded"
    MISMATCH = "mismatch"
    MISSING = "missing"
    BREAKING = "breaking"
    UNVERIFIED = "unverified"


class ModelRepoInventory(BaseModel):
    """Per-repo capability inventory produced in Phase 2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repository name.")
    repo_path: str = Field(description="Absolute path to the repository root.")
    pipeline_role: str = Field(
        default="",
        description="Human-readable description of the repo's role in the pipeline.",
    )
    kafka_produce_topics: tuple[str, ...] = Field(
        default=(),
        description="Kafka topics this repo produces to.",
    )
    kafka_consume_topics: tuple[str, ...] = Field(
        default=(),
        description="Kafka topics this repo subscribes to.",
    )
    db_tables_write: tuple[str, ...] = Field(
        default=(),
        description="Database tables this repo writes to.",
    )
    db_tables_read: tuple[str, ...] = Field(
        default=(),
        description="Database tables this repo reads from.",
    )
    entrypoint_command: str = Field(
        default="",
        description="Command that starts this service (Dockerfile CMD, compose command, etc.).",
    )
    entrypoint_status: str = Field(
        default="",
        description="REAL / STUB / MISSING — runtime entrypoint proof result.",
    )
    inventory_json: str = Field(
        default="",
        description="Full structured JSON inventory from the Phase 2 agent (for downstream phases).",
    )


class ModelGapFinding(BaseModel):
    """Single entry in the severity-ordered gap register (Phase 5 output)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: int = Field(description="Sequential 1-based finding number.")
    severity: EnumFindingSeverity = Field(description="Severity tier.")
    proof_category: EnumProofCategory = Field(
        description="Which of the six proof categories this finding belongs to.",
    )
    description: str = Field(
        description="Human-readable description of the integration gap.",
    )
    producer_repo: str = Field(
        default="",
        description="Repo that produces the relevant artifact (topic, table, model).",
    )
    consumer_repo: str = Field(
        default="",
        description="Repo that consumes or reads the artifact.",
    )
    evidence_location: str = Field(
        default="",
        description="File path and line number of the evidence (e.g. 'src/handler.py:42').",
    )
    proposed_fix: str = Field(
        default="",
        description="Suggested remediation action.",
    )
    status: EnumFindingStatus = Field(
        default=EnumFindingStatus.GAP,
        description="Proof verdict for this finding.",
    )


class ModelPipelineAuditResult(BaseModel):
    """Output of the pipeline audit orchestrator after all phases complete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_status: EnumPipelineAuditStatus = Field(
        description="Overall audit run status.",
    )
    repos_audited: tuple[str, ...] = Field(
        default=(),
        description="Repos included in the audit (post-discovery filtering).",
    )
    repo_inventories: tuple[ModelRepoInventory, ...] = Field(
        default=(),
        description="Per-repo capability inventories from Phase 2.",
    )
    gap_register: tuple[ModelGapFinding, ...] = Field(
        default=(),
        description=(
            "Severity-ordered gap register from Phase 5. "
            "Ordered: BREAKING → CRITICAL → HIGH → MEDIUM → LOW."
        ),
    )
    breaking_count: int = Field(
        default=0,
        description="Number of BREAKING findings.",
    )
    critical_count: int = Field(
        default=0,
        description="Number of CRITICAL findings.",
    )
    high_count: int = Field(
        default=0,
        description="Number of HIGH findings.",
    )
    medium_count: int = Field(
        default=0,
        description="Number of MEDIUM findings.",
    )
    low_count: int = Field(
        default=0,
        description="Number of LOW findings.",
    )
    tickets_created: tuple[str, ...] = Field(
        default=(),
        description="Linear ticket IDs created in Phase 6 (empty when skip_ticket_creation=True).",
    )
    dry_run: bool = Field(
        default=False,
        description="True when the run was executed in dry-run mode.",
    )
