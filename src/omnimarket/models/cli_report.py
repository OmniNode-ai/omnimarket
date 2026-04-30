"""Typed report contract for OmniMarket CLI output rendering."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumMarketCliOutputFormat(StrEnum):
    """Supported CLI report output formats."""

    TEXT = "text"
    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "markdown"


class EnumMarketCliVerbosity(StrEnum):
    """Supported CLI report verbosity levels."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    VERBOSE = "verbose"
    DEBUG = "debug"


class EnumMarketCliStatus(StrEnum):
    """Top-level CLI report status values."""

    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class ModelMarketCliStep(BaseModel):
    """One execution step in a CLI report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: str
    description: str = ""
    details: dict[str, object] = Field(default_factory=dict)


class ModelMarketCliEvidenceRef(BaseModel):
    """Reference to durable evidence associated with a CLI report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    ref: str
    description: str = ""


class ModelMarketCliInputSummary(BaseModel):
    """Summary of user or runtime inputs used for a CLI invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: dict[str, object] = Field(default_factory=dict)


class ModelMarketCliOutputConfig(BaseModel):
    """Output rendering configuration for a CLI report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: EnumMarketCliOutputFormat
    verbosity: EnumMarketCliVerbosity


class ModelMarketCliReport(BaseModel):
    """Authoritative CLI report model emitted by market-owned surfaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    node_name: str
    contract_name: str
    contract_version: str
    run_id: UUID
    correlation_id: UUID
    mode: str
    status: EnumMarketCliStatus
    input_summary: ModelMarketCliInputSummary
    steps: list[ModelMarketCliStep]
    evidence: list[ModelMarketCliEvidenceRef]
    result_summary: dict[str, object]
    terminal_event: str
    output_config: ModelMarketCliOutputConfig
    started_at: datetime
    completed_at: datetime
    duration_ms: int
