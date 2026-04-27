from pydantic import BaseModel, ConfigDict, Field


class ModelPrWatchOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="", description="Scope of the PR watch.")
