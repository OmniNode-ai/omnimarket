from pydantic import BaseModel, ConfigDict, Field


class ModelBusAuditComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="local", description="Scope of the bus audit.")
    registry_path: str | None = Field(
        default=None,
        description=(
            "Optional path to the event registry YAML. Defaults to the "
            "omnimarket emit-daemon topic registry."
        ),
    )
    contract_roots: list[str] = Field(
        default_factory=list,
        description="Repo or directory roots to scan for node contract.yaml files.",
    )
    failures_only: bool = Field(
        default=False, description="Return only ERROR-severity findings."
    )
    verbose: bool = Field(
        default=False, description="Include informational findings where available."
    )
    skip_daemon: bool = Field(
        default=False,
        description="Skip live emit-daemon checks. Current node performs static audit.",
    )
    broker: str | None = Field(
        default=None,
        description="Kafka broker hint for future effectful daemon sampling.",
    )
    sample_count: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Requested sample count for future effectful topic sampling.",
    )
    dry_run: bool = Field(default=False, description="Run without side effects.")


__all__ = ["ModelBusAuditComputeRequest"]
