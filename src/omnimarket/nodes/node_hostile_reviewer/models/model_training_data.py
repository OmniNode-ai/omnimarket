"""ModelTrainingDataRecord — labeled finding tuple for local model convergence.

Each review session produces labeled data: (code_diff, prompt, model_response,
frontier_label). Stored for offline fine-tuning. See design doc section
"Training Data and Local Model Convergence".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
)


class EnumLabelSource(StrEnum):
    FRONTIER_BOOTSTRAP = "frontier_bootstrap"
    HUMAN_VERIFIED = "human_verified"
    FIX_VERIFIED = "fix_verified"


class ModelTrainingDataRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID = Field(...)
    correlation_id: UUID = Field(...)
    session_id: UUID = Field(...)
    model_key: str = Field(...)
    category: EnumFindingCategory = Field(...)
    severity: EnumFindingSeverity = Field(...)
    code_diff_hash: str = Field(..., description="SHA256 of the code diff content.")
    prompt_hash: str = Field(..., description="SHA256 of the prompt sent.")
    model_response_hash: str = Field(
        ..., description="SHA256 of the raw model response."
    )
    local_detected: bool = Field(...)
    frontier_detected: bool = Field(...)
    label_source: EnumLabelSource = Field(...)
    recorded_at: datetime = Field(...)


__all__: list[str] = [
    "EnumLabelSource",
    "ModelTrainingDataRecord",
]
