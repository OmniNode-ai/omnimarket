# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model provider names shared by the setup and model-setup nodes (OMN-23413)."""

from __future__ import annotations

from typing import Literal

#: The models a developer chooses from, in the order onboarding lists them.
#: ``anthropic`` joined by operator ruling 2026-10-11T15:05:50Z (OMN-20990).
#: ``glm`` (OMN-20157): z.ai's general API, the catalogue's customer GLM plan.
#: ``local`` (OMN-21162): a model server on an OpenAI-compatible URL, no key.
ModelProvider = Literal[
    "gemini",
    "openrouter",
    "openai",
    "anthropic",
    "glm",
    "ollama",
    "local",
    # OMN-21263: any other endpoint that speaks the OpenAI API, by URL, with a key.
    "openai_compatible",
]
