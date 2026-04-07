# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Buildability classification enumeration for ticket triage."""

from __future__ import annotations

from enum import StrEnum


class EnumBuildability(StrEnum):
    """Classification of a ticket's buildability by the autonomous loop.

    Values:
        AUTO_BUILDABLE: Ticket can be fully executed by an agent without
            human intervention.
        NEEDS_ARCH_DECISION: Ticket requires architectural decisions or
            design review before implementation can proceed.
        BLOCKED: Ticket has explicit blockers (dependencies, missing
            information, external team coordination).
        SKIP: Ticket should be skipped in this cycle (already in progress,
            stale, or explicitly excluded).
    """

    AUTO_BUILDABLE = "auto_buildable"
    NEEDS_ARCH_DECISION = "needs_arch_decision"
    BLOCKED = "blocked"
    SKIP = "skip"
