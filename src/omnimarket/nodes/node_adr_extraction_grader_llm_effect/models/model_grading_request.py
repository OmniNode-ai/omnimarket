# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelGradingRequest -- input contract for node_adr_extraction_grader_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGradingRequest(BaseModel):
    """Command payload for a single ADR extraction grading call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ground_truth_adr: str = Field(
        ...,
        description="The authoritative ADR text used as ground truth for scoring.",
    )
    extraction_output: list[dict[str, object]] = Field(
        ...,
        description="Structured extraction output from the model under evaluation.",
    )
    source_document: str = Field(
        ...,
        description="Original source document from which ADRs were extracted.",
    )
    correlation_id: str = Field(
        ...,
        description="Correlation ID linking this grading call to the parent canary run.",
    )
    model_key_under_test: str = Field(
        ...,
        description="Short key identifying the extraction model being graded.",
    )


__all__: list[str] = ["ModelGradingRequest"]
