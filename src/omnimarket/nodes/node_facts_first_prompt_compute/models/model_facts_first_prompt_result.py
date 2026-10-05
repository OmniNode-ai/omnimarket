# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the facts-first prompt compute (OMN-19432)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelFactsFirstPromptResult(BaseModel):
    """The prompt to send, and whether any facts were stated ahead of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    facts_stated: bool


__all__ = ["ModelFactsFirstPromptResult"]
