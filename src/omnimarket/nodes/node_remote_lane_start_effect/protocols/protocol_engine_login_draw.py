# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam through which a lane start draws its engine login from the placed host's token pool (OMN-23425)."""

from __future__ import annotations

from typing import Protocol

from omnibase_spi.contracts.services import ModelLaneHostRef

from omnimarket.models.engine_login import (
    ModelEngineLoginDraw,
)


class ProtocolEngineLoginDraw(Protocol):
    def draw_login(self, host: ModelLaneHostRef) -> ModelEngineLoginDraw: ...


__all__ = ["ProtocolEngineLoginDraw"]
