from pydantic import BaseModel, ConfigDict, Field


class ModelDodCheckResult(BaseModel):
    """Result for a single DoD check against one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(
        description="Check identifier (e.g. contract_exists, pr_merged)."
    )
    status: str = Field(description="pass | fail | skip")
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Machine-readable check details.",
    )


class ModelDodTicketResult(BaseModel):
    """Aggregated DoD check results for a single ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Ticket ID.")
    status: str = Field(description="verified | failed | skipped")
    checks: tuple[ModelDodCheckResult, ...] = Field(default=())
    receipt_path: str = Field(
        default="", description="Written or planned receipt path."
    )
    receipt_written: bool = Field(default=False)
    failed: int = Field(default=0)
    skipped: int = Field(default=0)


class ModelDodSweepOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Sweep result status.")
    mode: str = Field(default="targeted", description="targeted | batch")
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
    # Batch mode fields
    batch_results: tuple[ModelDodTicketResult, ...] = Field(
        default=(),
        description="Per-ticket results in batch mode.",
    )
    batch_total: int = Field(default=0, description="Total tickets processed in batch.")
    batch_failed: int = Field(
        default=0, description="Tickets with at least one failed check."
    )
    batch_verified: int = Field(
        default=0, description="Tickets with all checks passing."
    )
