# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_two_backend_suite_effect: where the effect list comes from, how a factory is found."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from omnimarket.models.model_effect_backend_report import ModelReportRead
from omnimarket.protocols.protocol_effect_backend_report_source import (
    ProtocolEffectBackendReportSource,
)


class ProtocolDottedResolver(Protocol):
    def resolve(self, dotted: str) -> Callable[..., object]: ...


__all__ = [
    "ModelReportRead",
    "ProtocolDottedResolver",
    "ProtocolEffectBackendReportSource",
]
