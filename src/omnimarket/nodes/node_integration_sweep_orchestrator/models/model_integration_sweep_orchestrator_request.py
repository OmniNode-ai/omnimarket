from pydantic import BaseModel, ConfigDict, Field


class ModelIntegrationSweepOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="", description="Scope of the integration sweep.")
    tickets: list[str] = Field(
        default_factory=list,
        description="Explicit ticket IDs to include in the artifact.",
    )
    artifact_root: str = Field(
        default="",
        description=(
            "Optional root for drift/integration output. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    artifact_date: str = Field(
        default="",
        description="ISO date used in the artifact filename. Defaults to today.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, compute the artifact path but do not write it.",
    )
