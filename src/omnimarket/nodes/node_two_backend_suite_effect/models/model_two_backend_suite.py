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


class EnumTwoBackendVerdict(StrEnum):
    BOTH_PASS = "both_pass"
    ONE_BACKEND_ONLY = "one_backend_only"
    DIVERGED = "diverged"
    BOTH_FAIL = "both_fail"


class ModelEffectBackendEntry(BaseModel):
    """One effect node as the core validator reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    node_name: str
    path: str = ""
    has_in_memory_default: bool = False
    has_real_backend_overlay: bool = False
    in_memory_factory: str = ""
    real_factory: str = ""
    overlay_key: str = ""
    scenario: str = ""

    @property
    def on_both(self) -> bool:
        return self.has_in_memory_default and self.has_real_backend_overlay


class ModelEffectBackendReport(BaseModel):
    """The core validator's report; the counts are derived from the entries, never read back."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    entries: tuple[ModelEffectBackendEntry, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def not_on_both_count(self) -> int:
        return sum(1 for e in self.entries if not e.on_both)


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
