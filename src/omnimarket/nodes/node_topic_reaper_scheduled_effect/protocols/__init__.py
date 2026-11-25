# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_topic_reaper_scheduled_effect: where the reaper's evidence comes from."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_topic_reaper_scheduled_effect.models import (
    ModelReaperEvidence,
)


class ReaperEvidenceError(RuntimeError):
    """The broker or the contracts could not be read; the message is a typed reason."""


class ProtocolReaperEvidence(Protocol):
    def read(self) -> ModelReaperEvidence:
        """One reading of the broker; raises ``ReaperEvidenceError`` with a typed reason."""
        ...


__all__ = ["ModelReaperEvidence", "ProtocolReaperEvidence", "ReaperEvidenceError"]
