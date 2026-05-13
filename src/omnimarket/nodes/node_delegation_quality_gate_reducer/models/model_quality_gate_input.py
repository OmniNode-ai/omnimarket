# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Quality gate input model for the delegation pipeline.

Represents the input to the quality gate reducer: LLM response content,
expected quality markers, and optional contract-declared DoD check lists.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelQualityGateInput(BaseModel):
    """Gate input: LLM response content and expected quality markers.

    Attributes:
        correlation_id: Tracks this input back to the original request.
        task_type: The task classification for type-specific checks.
        llm_response_content: The raw LLM response to evaluate.
        expected_markers: Strings expected in the response for the task type.
        min_response_length: Minimum acceptable response length in characters.
        dod_deterministic: Deterministic DoD check names from the task-class
            contract (OMN-10614). These checks BLOCK delegation on failure.
            Supported: "output_parses", "signature_preserved".
        dod_heuristic: Heuristic DoD check names from the task-class contract
            (OMN-10614). These checks escalate per contract policy on failure.
            Supported: "no_refusal", "min_length_chars_N" (N is the threshold).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this input back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification for type-specific checks.",
    )
    llm_response_content: str = Field(
        ...,
        description="The raw LLM response to evaluate.",
    )
    expected_markers: tuple[str, ...] = Field(
        default=(),
        description="Strings expected in the response for the task type.",
    )
    min_response_length: int = Field(
        default=60,
        description="Minimum acceptable response length in characters.",
    )
    dod_deterministic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Deterministic DoD check names from the task-class contract (OMN-10614). "
            "These checks BLOCK delegation result injection on failure."
        ),
    )
    dod_heuristic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Heuristic DoD check names from the task-class contract (OMN-10614). "
            "These checks escalate per contract policy on failure."
        ),
    )


__all__: list[str] = ["ModelQualityGateInput"]
