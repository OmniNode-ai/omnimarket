from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPrCheckBucket(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    SKIPPING = "skipping"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


class ModelPrCheckStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Check or workflow name.")
    state: str = Field(default="", description="Raw gh check state.")
    bucket: EnumPrCheckBucket = Field(
        default=EnumPrCheckBucket.UNKNOWN,
        description="gh pr checks bucket classification.",
    )
    workflow: str = Field(default="", description="GitHub Actions workflow name.")
    link: str = Field(default="", description="Details URL for the check.")
    started_at: str = Field(default="", description="Raw gh startedAt value.")
    completed_at: str = Field(default="", description="Raw gh completedAt value.")


__all__ = ["EnumPrCheckBucket", "ModelPrCheckStatus"]
