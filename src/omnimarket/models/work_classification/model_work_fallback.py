# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared records of the lab-model fallback: its store key, stored result, route answer and store outcome."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelFallbackKey(BaseModel):
    """The fallback table's key: stable across calls and restarts. Frozen, so hashable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_digest: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    route_model_version: str = Field(min_length=1)


class ModelFallbackResult(BaseModel):
    """The stored fallback result: a work-type id and the route receipt. Never the text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_type_id: str = Field(min_length=1)
    route_receipt_id: str = Field(min_length=1)


class ModelRouteAnswer(BaseModel):
    """What the delegation route returns: the id it chose, its receipt, the host and model version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_type_id: str = Field(min_length=1)
    route_receipt_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    route_model_version: str = Field(min_length=1)


class EnumStoreStatus(StrEnum):
    """STORED: first write. SAME: an equal result already stored. REFUSED: a different one is."""

    STORED = "stored"
    SAME = "same"
    REFUSED = "refused"


class ModelStoreOutcome(BaseModel):
    """The answer of one atomic put-if-absent, carrying the result the store holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumStoreStatus
    stored: ModelFallbackResult


__all__ = [
    "EnumStoreStatus",
    "ModelFallbackKey",
    "ModelFallbackResult",
    "ModelRouteAnswer",
    "ModelStoreOutcome",
]
