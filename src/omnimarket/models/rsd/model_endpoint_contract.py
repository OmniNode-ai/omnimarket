# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared typed endpoint-registry contract, independent of any node package."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class EnumRsdEndpointCapability(StrEnum):
    """Canonical endpoint capabilities carried by the endpoint registry."""

    CODE_GENERATION = "code_generation"
    STRUCTURED_OUTPUT = "structured_output"
    REFACTORING = "refactoring"
    REASONING = "reasoning"
    ANALYSIS = "analysis"
    MATH = "math"
    PLANNING = "planning"
    SYNTHESIS = "synthesis"
    GENERAL = "general"
    EMBEDDINGS = "embeddings"


class ModelRsdEndpointContract(BaseModel):
    """Closed endpoint-registry row shared by offline route validators."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    base_url: str
    model_id: str
    provider: str
    capabilities: tuple[EnumRsdEndpointCapability, ...]
    context_window: int | None = None
    context_window_source: str = "unknown"
    cost_basis: str = "local"
    health_check_path: str = "/health"
    declared_by: str = ""
    declared_at: str = ""
    endpoint_ref: str = ""

    @field_validator("capabilities", mode="before")
    @classmethod
    def coerce_list(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


__all__ = ["EnumRsdEndpointCapability", "ModelRsdEndpointContract"]
