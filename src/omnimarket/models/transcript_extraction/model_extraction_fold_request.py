# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the extraction fold (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.transcript_extraction.model_extraction_verify_result import (
    ModelExtractionVerifyResult,
)


class ModelExtractionFoldRequest(BaseModel):
    """segment_results are the verifier results of a transcript's segments, in any order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_results: tuple[ModelExtractionVerifyResult, ...]
