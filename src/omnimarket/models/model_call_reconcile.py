# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for model-server request reconciliation (OMN-20299)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumServedRequestClass(StrEnum):
    ATTRIBUTED = "attributed"
    ORPHAN_CORRELATION_ID = "orphan_correlation_id"
    BYPASS = "bypass"


class ModelServedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str
    timestamp: str | None
    path: str
    status: int
    correlation_id: str | None


class ModelClassifiedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelServedRequest
    classification: EnumServedRequestClass
    caller_lane: str | None


class ModelReconcileReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    attributed: int
    orphan_correlation_id: int
    bypass: int
    attributed_by_lane: dict[str, int]
    bypass_by_server: dict[str, int]
    bypass_samples: list[ModelServedRequest]
