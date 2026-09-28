# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which of the four landing events a projection request carries."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingProjectionEventKind(StrEnum):
    """One member per topic node_pr_landing_orchestrator publishes (OMN-19824)."""

    TRANSITIONED = "transitioned"
    AGENT_NEEDED = "agent_needed"
    MERGED = "merged"
    CLOSED = "closed"


__all__: list[str] = ["EnumPrLandingProjectionEventKind"]
