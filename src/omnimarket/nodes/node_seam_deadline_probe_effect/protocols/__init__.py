# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_seam_deadline_probe_effect: send one command, read its correlated result by a deadline."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    ModelSeamAnswer,
    ModelSeamDemand,
)


class ProtocolSeamBus(Protocol):
    """Demand one command and report what came back by its deadline.

    A transport that cannot be opened or read answers ``bus-unreadable``; it never raises and never
    answers ``result`` for a reply it did not read.
    """

    def demand(self, demand: ModelSeamDemand) -> ModelSeamAnswer: ...


__all__ = ["ProtocolSeamBus"]
