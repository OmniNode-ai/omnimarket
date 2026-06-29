# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding models for node_dependency_health_sweep."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class EnumDepHealthFindingType(StrEnum):
    ORPHAN_IMPORT = "ORPHAN_IMPORT"
    MISSING_TOPIC_EDGE = "MISSING_TOPIC_EDGE"
    DEAD_IMPORT = "DEAD_IMPORT"
    UNTESTED_HANDLER = "UNTESTED_HANDLER"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    UNDECLARED_TOPIC = "UNDECLARED_TOPIC"


class EnumDepHealthSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    INFO = "INFO"


class ModelDepHealthFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_type: EnumDepHealthFindingType
    severity: EnumDepHealthSeverity
    repo: str
    file_path: str | None = None
    symbol: str | None = None
    detail: str
    rule_id: str
    rule_version: str

    @field_validator("file_path")
    @classmethod
    def _sanitize_file_path(cls, value: str | None) -> str | None:
        if value is None or value.startswith("src/"):
            return value
        marker = "/src/"
        if marker in value:
            return "src/" + value.rsplit(marker, maxsplit=1)[1]
        return value
