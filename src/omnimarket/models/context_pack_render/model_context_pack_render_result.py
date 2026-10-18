# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of a context-pack render (OMN-21109, plan task TA.7)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_pack_render.enum_context_pack_render_failure import (
    EnumContextPackRenderFailure,
)
from omnimarket.models.context_pack_render.enum_context_pack_render_status import (
    EnumContextPackRenderStatus,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)


class ModelContextPackRenderResult(BaseModel):
    """messages is the ordered body; the first stable_prefix_message_count of them form the prefix.

    body_hash is sha256 over the canonical JSON of the role/text list. stable_prefix_hash is
    sha256 over the same encoding of the legend and the stable blocks only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextPackRenderStatus
    messages: tuple[ModelContextPackRenderMessage, ...] = ()
    body_hash: str | None = None
    stable_prefix_hash: str | None = None
    stable_prefix_message_count: int = Field(default=0, ge=0)
    failure_class: EnumContextPackRenderFailure | None = None
    errors: tuple[str, ...] = ()
