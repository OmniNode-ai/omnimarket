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
    contracts_dir: str = Field(
        default="",
        description="Optional directory containing contracts/<ticket>.yaml. Defaults to artifact_root/contracts.",
    )
    receipts_dir: str = Field(
        default="",
        description="Optional directory for drift/dod_receipts. Defaults to artifact_root/drift/dod_receipts.",
    )
    runtime_host: str = Field(
        default="192.168.86.201",  # onex-allow-internal-ip OMN-9334 reason="default runtime host for SHA probe; overridden by caller or env; not a shipping connection string"
        description="Runtime SSH host for runtime_sha_match probes.",
    )
    runtime_repo_path: str = Field(
        default="/data/omninode/omni_home/omnimarket",
        description="Repo path on the runtime host used by the phase-1 SSH git SHA probe.",
    )
    artifact_date: str = Field(
        default="",
        description="ISO date used in the artifact filename. Defaults to today.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, compute the artifact path but do not write it.",
    )
