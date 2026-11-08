# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the effect-backend report comes from, shared by the two-backend suite and the reading writer (OMN-22911)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_effect_backend_report import ModelReportRead


class ProtocolEffectBackendReportSource(Protocol):
    def read(self) -> ModelReportRead: ...


__all__ = ["ProtocolEffectBackendReportSource"]
