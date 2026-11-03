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
from omnimarket.models.claude_code_binding.model_compaction_client_binding import (
    ModelCompactionClientBinding,
)


class ModelBindingRenderRequest(BaseModel):
    """What a render needs: the rows and the host's client-section maximum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bindings: tuple[ModelCompactionClientBinding | ModelClientBinding, ...]
    max_mode: EnumClientMaxMode
