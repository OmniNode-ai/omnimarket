# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelRawEvidencePayload — collected raw evidence from external sources."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRawEvidencePayload(BaseModel):
    """Normalized raw evidence gathered by the evidence collector effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    repository: str = Field(..., min_length=1)
    source_commit_sha: str = Field(..., min_length=7)
    collected_at: str = Field(..., min_length=1)
    collector_identity: str = Field(..., min_length=1)
    source_surfaces: tuple[str, ...] = Field(..., min_length=1)
    source_pr: int | None = Field(default=None, ge=1)
    source_ci_run: str | None = Field(default=None, min_length=1)
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    diff_refs: tuple[str, ...] = Field(default_factory=tuple)
    ci_artifact_refs: tuple[str, ...] = Field(default_factory=tuple)
    test_output_refs: tuple[str, ...] = Field(default_factory=tuple)
    raw_payload_refs: tuple[str, ...] = Field(default_factory=tuple)
    provenance: Mapping[str, str] = Field(default_factory=dict)


__all__: list[str] = ["ModelRawEvidencePayload"]
