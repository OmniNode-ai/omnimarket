# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelSegmentationRequest -- input contract for node_adr_segmentation_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSegmentationRequest(BaseModel):
    """Command payload for a single document semantic segmentation call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str = Field(
        ...,
        description="Repository-relative path to the source document.",
    )
    source_content: str = Field(
        ...,
        description="Full text content of the document to segment.",
    )
    source_content_sha256: str = Field(
        ...,
        description="SHA-256 hex digest of source_content, pre-computed by the caller.",
    )
    correlation_id: str = Field(
        ...,
        description="Correlation ID linking this segmentation call to a parent pipeline run.",
    )


__all__: list[str] = ["ModelSegmentationRequest"]
