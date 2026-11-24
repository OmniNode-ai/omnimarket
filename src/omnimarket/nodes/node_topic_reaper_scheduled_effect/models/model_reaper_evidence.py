# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the broker and the contracts say, as the reaper's request needs it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_topic_reaper import (
    ModelReaperObservation,
)


class ModelReaperEvidence(BaseModel):
    """One broker reading folded into the reaper's observations, with the topics a contract declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelReaperObservation, ...]
    #: Topics any contract declares; the reaper never flags one.
    boot_set_topics: tuple[str, ...]


__all__: list[str] = ["ModelReaperEvidence"]
