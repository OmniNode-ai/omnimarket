# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated result of local access-mode invocation."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_operator_access_mode_effect.models.enum_access_mode import (
    EnumAccessMode,
)


class ModelLocalAccessModeResult(BaseModel):
    """Resolved mode and the detector's explanation of its choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumAccessMode
    source: str
    detected: EnumAccessMode | None
    signals: tuple[str, ...] = ()
