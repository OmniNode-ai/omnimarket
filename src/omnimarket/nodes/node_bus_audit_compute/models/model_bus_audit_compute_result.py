from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBusAuditStatus(StrEnum):
    CLEAN = "clean"
    FINDINGS = "findings"
    ERROR = "error"


class EnumBusAuditFindingType(StrEnum):
    INVALID_TOPIC_NAME = "invalid_topic_name"
    DUPLICATE_TOPIC = "duplicate_topic"
    MISSING_FAN_OUT = "missing_fan_out"
    MISSING_PARTITION_KEY = "missing_partition_key"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    CONTRACT_TOPIC_UNREGISTERED = "contract_topic_unregistered"
    CONTRACT_EVENT_BUS_MISSING = "contract_event_bus_missing"
    REGISTRY_NOT_FOUND = "registry_not_found"


class EnumBusAuditSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class ModelBusAuditFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_type: EnumBusAuditFindingType
    severity: EnumBusAuditSeverity
    subject: str
    message: str
    source_path: str = ""


class ModelBusAuditTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_name: str
    topic: str
    partition_key_field: str = ""
    required_fields: list[str] = Field(default_factory=list)


class ModelBusAuditComputeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EnumBusAuditStatus = Field(
        default=EnumBusAuditStatus.CLEAN, description="Audit result status."
    )
    run_id: str = Field(default="", description="Deterministic audit run identifier.")
    message: str = Field(default="", description="Human-readable status summary.")
    scope: str = Field(default="local", description="Audited scope.")
    dry_run: bool = False
    daemon_check: str = Field(
        default="static_only",
        description="Live daemon sampling status. Static-only until effect node lands.",
    )
    topics_registered: int = 0
    topics_declared: int = 0
    contracts_checked: int = 0
    findings: list[ModelBusAuditFinding] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "WARNING")


__all__ = [
    "EnumBusAuditFindingType",
    "EnumBusAuditSeverity",
    "EnumBusAuditStatus",
    "ModelBusAuditComputeResult",
    "ModelBusAuditFinding",
    "ModelBusAuditTopic",
]
