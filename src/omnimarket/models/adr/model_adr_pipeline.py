# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared ADR pipeline models for cross-node communication.

These types are owned by the orchestrator layer and passed through protocol
interfaces. Sub-nodes translate to/from their own private request models
internally. No node imports another node's private models.

[OMN-10698]
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from omnibase_core.models.adr.model_adr_draft import ModelADRDraft
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumAdrSourceVisibility(StrEnum):
    """Declared visibility of the repository that owns the extracted source."""

    public = "public"
    private = "private"


class EnumAdrPublicationClassification(StrEnum):
    """Publication sensitivity declared by the source-owning workflow."""

    public = "public"
    private = "private"
    restricted = "restricted"
    needs_review = "needs_review"


class EnumAdrKBDestination(StrEnum):
    """Closed set of knowledge-base publication destinations."""

    public = "public"
    private = "private"


class ModelAdrSourceProvenance(BaseModel):
    """Source identity and publication classification stated by the source owner.

    This fact is never inferred from a checkout path, remote URL, or repository
    name. It must be carried from the manifest into durable candidate evidence
    and repeated by the publish command so the publisher can detect a mismatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repository: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="Canonical owner/repository identity supplied by the source owner.",
    )
    source_visibility: EnumAdrSourceVisibility
    publication_classification: EnumAdrPublicationClassification

    @model_validator(mode="after")
    def _reject_conflicting_classification(self) -> ModelAdrSourceProvenance:
        if (
            self.source_visibility is EnumAdrSourceVisibility.private
            and self.publication_classification
            is EnumAdrPublicationClassification.public
        ):
            raise ValueError(
                "private source visibility conflicts with public publication classification"
            )
        return self


class ModelAdrSourceDocumentEvidence(BaseModel):
    """Hash-pinned, repository-relative source document evidence.

    The publisher deliberately consumes hash-only evidence: source bytes are
    re-opened and verified by the ingestion/segmentation boundary before LLM
    extraction, while the publisher may run later on a different machine with
    no authority to reopen the private source checkout.  The durable SHA-256 is
    therefore the publication trust boundary, not a claim that the publisher
    revalidated machine-local files.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str = Field(..., min_length=1)
    source_content_sha256: str = Field(
        ..., pattern=r"^[a-f0-9]{64}$", description="SHA-256 of the full source bytes."
    )

    @field_validator("source_path")
    @classmethod
    def _require_repository_relative_path(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("source_path must be repository-relative")
        return value


class ModelAdrPublicationCandidate(BaseModel):
    """Durable canary evidence consumed by the KB publisher.

    Legacy evidence may omit provenance, but it is not publishable: the
    publisher fails closed before any subprocess boundary in that case.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: ModelADRDraft
    source_provenance: ModelAdrSourceProvenance | None = None
    kb_destination: EnumAdrKBDestination | None = Field(
        default=None,
        description=(
            "Manifest-owned closed KB destination. Omission is legacy evidence "
            "and is rejected by the publisher before subprocess execution."
        ),
    )
    source_documents: tuple[ModelAdrSourceDocumentEvidence, ...] = Field(
        default_factory=tuple
    )


class ModelAdrDocumentRef(BaseModel):
    """Minimal document reference produced by the ingestion protocol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str = Field(..., description="Relative path of the document.")
    repo_name: str = Field(default="", description="Repository name.")
    file_size_bytes: int = Field(default=0, ge=0)
    source_content_sha256: str = Field(default="")


class ModelAdrIngestionResult(BaseModel):
    """Result returned by the ingestion protocol to the orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: list[ModelAdrDocumentRef] = Field(default_factory=list)
    root_paths: list[str] = Field(default_factory=list)


class ModelAdrDocumentSegment(BaseModel):
    """Immutable semantic segment passed from segmentation to extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str = Field(..., description="Deterministic segment identifier.")
    source_path: str = Field(..., description="Workspace-relative source path.")
    source_content_sha256: str = Field(
        ..., description="SHA-256 digest of the full source document."
    )
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)
    segment_type: str = Field(..., description="Semantic segment classification.")
    content: str = Field(..., description="Verbatim segment content.")
    segment_content_sha256: str = Field(
        ..., description="SHA-256 digest of the segment content."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_line_span(self) -> ModelAdrDocumentSegment:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ModelAdrSegmentationResult(BaseModel):
    """Entry-level segmentation result returned to the ADR orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(...)
    segments: tuple[ModelAdrDocumentSegment, ...] = Field(default_factory=tuple)
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_success_failure_shape(self) -> ModelAdrSegmentationResult:
        if self.success:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError(
                    "error_code and error_message must be unset when success=True"
                )
            return self
        if not self.error_code or not self.error_message:
            raise ValueError(
                "error_code and error_message are required when success=False"
            )
        return self


class ModelAdrExtractionSummary(BaseModel):
    """Minimal extraction result the orchestrator needs for grading and draft-gen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(...)
    model_key: str = Field(...)
    model_id: str = Field(
        default="",
        description="Canonical served model identifier from the manifest route.",
    )
    pipeline_version: str = Field(
        default="",
        description="Canonical pipeline version that produced this extraction.",
    )
    extraction_count: int = Field(default=0, ge=0)
    extractions_raw: list[dict[str, object]] = Field(
        default_factory=list,
        description="Raw serialized extraction dicts for downstream grading.",
    )
    first_extraction_json: str = Field(
        default="",
        description="JSON of the first extraction, for draft-gen input.",
    )
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


class ModelAdrGradingScores(BaseModel):
    """Grading scores returned by the grader protocol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(...)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    precision: float | None = Field(default=None, ge=0.0, le=1.0)
    fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    format_compliance: float | None = Field(default=None, ge=0.0, le=1.0)
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    latency_ms: int = Field(default=0, ge=0)


class ModelAdrManifestModel(BaseModel):
    """A single model configuration declared in the ground truth manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., description="Short identifier (e.g. 'qwen3-coder').")
    provider: str = Field(default="local")
    model_id: str = Field(..., description="Exact model ID for inference bridge.")
    external: bool = Field(default=False)


class ModelAdrManifestEntry(BaseModel):
    """One evaluation unit in the ground truth manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., description="Unique slug for this entry.")
    root_paths: list[str] = Field(..., min_length=1)
    ground_truth_adr: str | None = Field(
        default=None,
        description=(
            "Authoritative ADR text. Required for benchmark (ground-truth) "
            "entries; omitted for discovery entries where no ground truth exists."
        ),
    )
    models: list[ModelAdrManifestModel] = Field(..., min_length=1)
    ground_truth_adr_hash: str | None = Field(default=None)
    source_file_hash: str | None = Field(default=None)
    manifest_schema_version: str | None = Field(default=None)
    expected_decision_types: list[str] = Field(default_factory=list)
    expected_keywords: list[str] = Field(default_factory=list)
    source_confidence: str | None = Field(default=None)
    curation_notes: str | None = Field(default=None)
    # Discovery-mode fields (OMN-14103): discovery entries target sources with
    # no ground-truth ADR — the pipeline extracts candidate ADRs for human review.
    discovery_mode: bool = Field(
        default=False,
        description="When true, no ground_truth_adr is required and grading is skipped.",
    )
    source_directory: str | None = Field(default=None)
    topic_cluster: str | None = Field(default=None)
    rationale: str | None = Field(default=None)
    source_provenance: ModelAdrSourceProvenance | None = Field(
        default=None,
        description=(
            "Source-owned repository identity and publication classification. "
            "It is carried into candidate evidence; omission makes publication fail closed."
        ),
    )
    kb_destination: EnumAdrKBDestination | None = Field(
        default=None,
        description=(
            "Manifest-owned closed KB destination. It travels with every "
            "candidate and must match the publish request."
        ),
    )

    @model_validator(mode="after")
    def _require_ground_truth_for_benchmark_entries(self) -> ModelAdrManifestEntry:
        if not self.discovery_mode and not (
            self.ground_truth_adr and self.ground_truth_adr.strip()
        ):
            raise ValueError(
                f"manifest entry {self.id!r}: ground_truth_adr is required for "
                "benchmark entries (discovery_mode is false)."
            )
        return self

    @field_validator("models", mode="before")
    @classmethod
    def _normalize_model_entries(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value

        normalized: list[object] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(
                    {
                        "key": item,
                        "provider": "local",
                        "model_id": item,
                        "external": False,
                    }
                )
            else:
                normalized.append(item)
        return normalized


class ModelAdrRunRequest(BaseModel):
    """Orchestrator-internal per-entry run descriptor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    entry: ModelAdrManifestEntry
    output_dir: str
    allow_external_providers: bool = False
    model_subset: list[str] | None = None


__all__: list[str] = [
    "EnumAdrKBDestination",
    "EnumAdrPublicationClassification",
    "EnumAdrSourceVisibility",
    "ModelAdrDocumentRef",
    "ModelAdrDocumentSegment",
    "ModelAdrExtractionSummary",
    "ModelAdrGradingScores",
    "ModelAdrIngestionResult",
    "ModelAdrManifestEntry",
    "ModelAdrManifestModel",
    "ModelAdrPublicationCandidate",
    "ModelAdrRunRequest",
    "ModelAdrSegmentationResult",
    "ModelAdrSourceDocumentEvidence",
    "ModelAdrSourceProvenance",
]
