# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the extraction verifier (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.transcript_extraction.model_captured_prompt import (
    ModelCapturedPrompt,
)


class ModelExtractionVerifyRequest(BaseModel):
    """prompts are the captured records of the segment; proposals are the model's raw output.

    Each proposal is the JSON text of one proposed event, as the delegation call returned
    it, so a proposal that does not parse is counted apart from a quote that is absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: tuple[ModelCapturedPrompt, ...]
    proposals: tuple[str, ...]
