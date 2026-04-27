from pydantic import BaseModel, ConfigDict, Field


class ModelPrWatchOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="PR watch result status.")
