# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bindings and the installing host's maximum mode (OMN-18531)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.claude_code_binding.enum_client_max_mode import (
    EnumClientMaxMode,
)
from omnimarket.models.claude_code_binding.model_client_binding import (
    ModelClientBinding,
)


class ModelBindingRenderRequest(BaseModel):
    """What a render needs: the rows and the host's client-section maximum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bindings: tuple[ModelClientBinding, ...]
    max_mode: EnumClientMaxMode
