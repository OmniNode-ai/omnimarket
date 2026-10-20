# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adapter's typed result and the hook output shape it prints (OMN-18531)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_claude_code_hook_adapter_effect.models.model_client_verdict import (
    ModelClientVerdict,
)


class ModelHookAdapterResult(BaseModel):
    """client_verdict plus the hook's own output shape (None prints nothing)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID = Field(description="The id minted for this invocation.")
    client_verdict: ModelClientVerdict
    hook_output: dict[str, JsonValue] | None = None
