# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and results of the lane report relay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ModelLaneReportRelayRequest:
    """``target`` is the host's ssh target, ``remote_registry_root`` its registry root (None: find it)."""

    target: str
    remote_registry_root: str | None
    lane: str
    local_registry_root: str
    host_name: str
    result_text: str = ""
    run: Callable[..., Any] | None = None
    now: datetime | None = None


@dataclass(frozen=True)
class ModelLaneReportRelayResult:
    copied: list[str] = field(default_factory=list)
    already_local: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    from_result: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def line(self) -> str:
        return (
            f"copied={len(self.copied)} already_local={len(self.already_local)} "
            f"missing={len(self.missing)} from_result={self.from_result} error={self.error}"
        )
