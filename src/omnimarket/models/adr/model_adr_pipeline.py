# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared ADR pipeline models for cross-node communication.

These types are owned by the orchestrator layer and passed through protocol
interfaces. Sub-nodes translate to/from their own private request models
internally. No node imports another node's private models.

[OMN-10698]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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


class ModelAdrExtractionSummary(BaseModel):
    """Minimal extraction result the orchestrator needs for grading and draft-gen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(...)
    model_key: str = Field(...)
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
    ground_truth_adr: str = Field(..., description="Authoritative ADR text.")
    models: list[ModelAdrManifestModel] = Field(..., min_length=1)


class ModelAdrRunRequest(BaseModel):
    """Orchestrator-internal per-entry run descriptor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    entry: ModelAdrManifestEntry
    output_dir: str
    allow_external_providers: bool = False
    model_subset: list[str] | None = None


__all__: list[str] = [
    "ModelAdrDocumentRef",
    "ModelAdrExtractionSummary",
    "ModelAdrGradingScores",
    "ModelAdrIngestionResult",
    "ModelAdrManifestEntry",
    "ModelAdrManifestModel",
    "ModelAdrRunRequest",
]
