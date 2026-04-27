from pydantic import BaseModel, ConfigDict, Field


class ModelDodSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
