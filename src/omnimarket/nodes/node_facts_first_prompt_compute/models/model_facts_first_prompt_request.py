# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the facts-first prompt compute (OMN-19432)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.inference.task_class_authority import ModelFactsFirstPromptPolicy


class ModelFactsFirstPromptRequest(BaseModel):
    """A prompt and the declared policy that says what to compute from it.

    The caller resolves the policy from the task-class contract; the handler
    reads no file, so the same request always yields the same result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    policy: ModelFactsFirstPromptPolicy


__all__ = ["ModelFactsFirstPromptRequest"]
