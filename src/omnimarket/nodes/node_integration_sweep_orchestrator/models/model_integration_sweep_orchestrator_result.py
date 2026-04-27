from pydantic import BaseModel, ConfigDict, Field


class ModelIntegrationSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
