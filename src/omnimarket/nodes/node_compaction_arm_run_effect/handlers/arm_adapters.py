# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams to a headless Claude Code session and to the pinned pack build.

The handler never starts either itself: the live implementations are injected, tests inject
scripted ones. With no implementation the headless premises are ``UNVERIFIED`` and
``stock_rerun`` is ``REFUSED``, and the ``mod`` arm is ``REFUSED`` with a typed reason.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelAdapterOutput,
    ModelFork,
)

__all__ = ["ProtocolArmAdapter", "ProtocolHeadlessPremises"]


class ProtocolArmAdapter(Protocol):
    def compact(self, fork: ModelFork) -> ModelAdapterOutput | None:
        """Compact the fork at its boundary; None when the compactor produced nothing."""
        ...


class ProtocolHeadlessPremises(Protocol):
    def manual_compaction_accepted(self, fork: ModelFork) -> bool | None:
        """Whether a headless session accepts a manual compaction at the boundary; None if unknown."""
        ...

    def fork_keeps_transcript(self, fork: ModelFork) -> bool | None:
        """Whether a forked session keeps its transcript; None if unknown."""
        ...
