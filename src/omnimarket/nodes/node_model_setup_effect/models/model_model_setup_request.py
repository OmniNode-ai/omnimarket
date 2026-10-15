# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model setup effect is asked to do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The models a developer chooses from, in the order onboarding lists them.
#: ``anthropic`` joined by operator ruling 2026-10-11T15:05:50Z (OMN-20990).
ModelProvider = Literal["gemini", "openrouter", "openai", "anthropic", "ollama"]


class ModelModelSetupRequest(BaseModel):
    """``status``: every provider, set up or not, with its last test.
    ``test``: one pinned delegation for ``provider``, or for every set-up
    provider when it is ``None``. ``forget``: drop ``provider``'s last test,
    after its key is removed.

    OMN-21059, on the ordered models a stored key may serve (``provider`` and
    ``models`` required): ``add_models`` appends ``models`` after the stored
    ones; ``reorder`` replaces the list with ``models``, which must name exactly
    the stored set; ``remove_model`` drops the one model in ``models``.

    OMN-20844 (``provider`` required): ``default_model`` reads the model onboarding
    registers for ``provider`` from the deployment overlay; nothing is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal[
        "status",
        "test",
        "forget",
        "add_models",
        "reorder",
        "remove_model",
        "default_model",
    ]
    provider: ModelProvider | None = None
    models: tuple[str, ...] = ()


__all__ = ["ModelModelSetupRequest", "ModelProvider"]
