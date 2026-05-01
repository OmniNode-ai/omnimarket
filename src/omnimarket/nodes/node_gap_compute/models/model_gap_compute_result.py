from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    EnumGapSubcommand,
)


class EnumGapStatus(StrEnum):
    CLEAN = "clean"
    FINDINGS = "findings"
    BLOCKED = "blocked"
    ERROR = "error"


class EnumGapCategory(StrEnum):
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    MISSING_TEST = "MISSING_TEST"
    ARCHITECTURE_VIOLATION = "ARCHITECTURE_VIOLATION"
    MISSING_NODE_TYPE = "MISSING_NODE_TYPE"
    UNCOVERED_REQUIREMENT = "UNCOVERED_REQUIREMENT"
    INTEGRATION_HEALTH = "INTEGRATION_HEALTH"


class EnumGapConfidence(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    BEST_EFFORT = "BEST_EFFORT"
    SKIP = "SKIP"


class EnumGapSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class ModelGapFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str
    category: EnumGapCategory
    boundary_kind: str
    rule_name: str
    severity: EnumGapSeverity
    confidence: EnumGapConfidence
    repo: str
    path: str
    message: str
    proof: dict[str, object] = Field(default_factory=dict)


class ModelSkippedGapProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: str
    reason: str


class ModelGapComputeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EnumGapStatus = EnumGapStatus.CLEAN
    run_id: str = ""
    message: str = ""
    subcommand: EnumGapSubcommand = EnumGapSubcommand.DETECT
    scope: str = "local"
    dry_run: bool = False
    repos_in_scope: list[str] = Field(default_factory=list)
    contracts_checked: int = 0
    findings: list[ModelGapFinding] = Field(default_factory=list)
    best_effort_findings: list[ModelGapFinding] = Field(default_factory=list)
    skipped_probes: list[ModelSkippedGapProbe] = Field(default_factory=list)
    report_path: str | None = None
    dispatch_class_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def finding_count(self) -> int:
        return len(self.findings) + len(self.best_effort_findings)


__all__ = [
    "EnumGapCategory",
    "EnumGapConfidence",
    "EnumGapSeverity",
    "EnumGapStatus",
    "ModelGapComputeResult",
    "ModelGapFinding",
    "ModelSkippedGapProbe",
]
