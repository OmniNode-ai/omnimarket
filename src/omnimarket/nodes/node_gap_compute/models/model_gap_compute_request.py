from pydantic import BaseModel, ConfigDict, Field


class ModelGapComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="", description="Scope of the gap analysis.")
