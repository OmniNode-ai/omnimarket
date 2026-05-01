from pydantic import BaseModel, ConfigDict, Field


class ModelDodSweepOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(
        default="",
        description="Ticket ID or sweep scope. Targeted ticket IDs write receipts.",
    )
    evidence_root: str = Field(
        default="",
        description=(
            "Optional root for .evidence output. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    contract_root: str = Field(
        default="",
        description=(
            "Optional root containing contracts/. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, compute the receipt path but do not write it.",
    )
