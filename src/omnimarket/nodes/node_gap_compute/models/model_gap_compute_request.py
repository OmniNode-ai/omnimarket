from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumGapSubcommand(StrEnum):
    DETECT = "detect"
    FIX = "fix"
    CYCLE = "cycle"
    RECONCILE = "reconcile"


class EnumGapSeverityThreshold(StrEnum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ModelGapComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subcommand: EnumGapSubcommand = Field(default=EnumGapSubcommand.DETECT)
    scope: str = Field(default="local", description="Scope of the gap analysis.")
    epic: str | None = None
    report: str | None = None
    repo: str | None = None
    repo_roots: list[str] = Field(default_factory=list)
    since_days: int = Field(default=30, ge=1, le=3650)
    severity_threshold: EnumGapSeverityThreshold = EnumGapSeverityThreshold.WARNING
    max_findings: int = Field(default=200, ge=1, le=5000)
    max_best_effort: int = Field(default=50, ge=0, le=1000)
    max_iterations: int = Field(default=3, ge=1, le=20)
    output: str = Field(default="json", pattern="^(json|md)$")
    ticket: str | None = None
    latest: bool = False
    mode: str = Field(default="ticket-pipeline")
    choose: str | None = None
    force_decide: bool = False
    resume: str | None = None
    audit: bool = False
    no_fix: bool = False
    verify: bool = False
    auto_only: bool = False
    skip_infra_probes: bool = False
    include_auth_probes: bool = False
    lag_threshold: int = Field(default=10000, ge=0)
    dry_run: bool = False


__all__ = [
    "EnumGapSeverityThreshold",
    "EnumGapSubcommand",
    "ModelGapComputeRequest",
]
