# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding models for node_dependency_health_sweep."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
