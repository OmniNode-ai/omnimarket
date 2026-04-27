from pydantic import BaseModel, ConfigDict, Field


class ModelGapComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Gap analysis result status.")
