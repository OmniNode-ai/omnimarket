# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and results of the lane report relay.

Both are pydantic models so the runtime's handler wiring can resolve the handler's
input model from its ``handle()`` annotation and validate a bus demand into it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneReportRelayRequest(BaseModel):
    """``target`` is the host's ssh target, ``remote_registry_root`` its registry root (None: find it).

    ``run`` and ``now`` are in-process seams (an ssh runner and a clock) and never travel on the wire.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    target: str
    remote_registry_root: str | None = None
    lane: str
    local_registry_root: str
    host_name: str
    result_text: str = ""
    run: Callable[..., Any] | None = Field(default=None, exclude=True)
    now: datetime | None = None


class ModelLaneReportRelayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    copied: list[str] = Field(default_factory=list)
    already_local: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    from_result: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @property
    def line(self) -> str:
        return (
            f"copied={len(self.copied)} already_local={len(self.already_local)} "
            f"missing={len(self.missing)} from_result={self.from_result} error={self.error}"
        )
