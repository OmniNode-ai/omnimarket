# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model setup effect is asked to do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The models a developer chooses from, in the order onboarding lists them.
ModelProvider = Literal["gemini", "openrouter", "openai", "ollama"]


class ModelModelSetupRequest(BaseModel):
    """``status``: every provider, set up or not, with its last test.
    ``test``: one pinned delegation for ``provider``, or for every set-up
    provider when it is ``None``. ``forget``: drop ``provider``'s last test,
    after its key is removed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["status", "test", "forget"]
    provider: ModelProvider | None = None


__all__ = ["ModelModelSetupRequest", "ModelProvider"]
