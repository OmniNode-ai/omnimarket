from pydantic import BaseModel, ConfigDict, Field


class ModelIntegrationSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
    artifact_path: str = Field(
        default="", description="Written or planned artifact path."
    )
    artifact_written: bool = Field(
        default=False, description="Whether an artifact was written."
    )
    ticket_count: int = Field(default=0, description="Number of ticket IDs included.")
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Machine-readable sweep details.",
    )
