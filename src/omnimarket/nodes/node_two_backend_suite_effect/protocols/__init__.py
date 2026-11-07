# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_two_backend_suite_effect: where the effect list comes from, how a factory is found."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendReport,
)


class ModelReportRead(BaseModel):
    """The report, or the reason it could not be read. An error is never an empty list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: ModelEffectBackendReport | None = None
    error: str | None = None


class ProtocolEffectBackendReportSource(Protocol):
    def read(self) -> ModelReportRead: ...


class ProtocolDottedResolver(Protocol):
    def resolve(self, dotted: str) -> Callable[..., object]: ...


__all__ = [
    "ModelReportRead",
    "ProtocolDottedResolver",
    "ProtocolEffectBackendReportSource",
]
