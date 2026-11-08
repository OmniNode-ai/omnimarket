# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the two-backend suite (OMN-22911, plan section 3.6, piece 4).

``ModelEffectBackendReport`` is the core effect-backends validator's report as JSON: the list of
effect nodes with what each declares. The suite runs the ones declared on both backends and counts
the rest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_effect_backend_report import (
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
)


class EnumTwoBackendVerdict(StrEnum):
    BOTH_PASS = "both_pass"
    ONE_BACKEND_ONLY = "one_backend_only"
    DIVERGED = "diverged"
    BOTH_FAIL = "both_fail"


class ModelTwoBackendRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)


class ModelEffectRun(BaseModel):
    """One listed effect run on both backends: ``pass`` or ``fail:<ErrorType>`` per backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    verdict: EnumTwoBackendVerdict
    in_memory: str
    real: str


class ModelTwoBackendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    runs: tuple[ModelEffectRun, ...] = ()
    not_on_both_count: int = 0
    failure: str = ""

    @property
    def red(self) -> bool:
        return any(r.verdict is not EnumTwoBackendVerdict.BOTH_PASS for r in self.runs)


__all__ = [
    "EnumTwoBackendVerdict",
    "ModelEffectBackendEntry",
    "ModelEffectBackendReport",
    "ModelEffectRun",
    "ModelTwoBackendRequest",
    "ModelTwoBackendResult",
]
