from pydantic import BaseModel, ConfigDict, Field


class ModelDodSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
    ticket_id: str = Field(
        default="", description="Target ticket ID for targeted sweeps."
    )
    receipt_path: str = Field(
        default="", description="Written or planned receipt path."
    )
    receipt_written: bool = Field(
        default=False, description="Whether a receipt was written."
    )
    contract_path: str = Field(default="", description="Resolved ticket contract path.")
    contract_exists: bool = Field(
        default=False, description="Whether the ticket contract exists."
    )
    failed: int = Field(default=0, description="Number of failed deterministic checks.")
    skipped: int = Field(
        default=0, description="Number of skipped deterministic checks."
    )
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Machine-readable sweep details.",
    )
