# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler input: the request payload paired with the access read from the envelope."""

from __future__ import annotations

from omnibase_core.models.context.model_context_access import ModelContextAccess
from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_pack_orchestrator.model_context_pack_request import (
    ModelContextPackRequest,
)


class ModelContextPackCommand(BaseModel):
    """``access`` comes from the command envelope (plan section 4.3, Access), never the payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelContextPackRequest
    access: ModelContextAccess


__all__ = ["ModelContextPackCommand"]
