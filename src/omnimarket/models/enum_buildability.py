# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Buildability classification enumeration for ticket triage.

Related:
    - OMN-7312: ModelTicketClassification
    - OMN-5113: Autonomous Build Loop epic
    - OMN-7752: Migrated from omnibase_infra
"""

from __future__ import annotations

from enum import Enum


class EnumBuildability(str, Enum):
    """Classification of a ticket's buildability by the autonomous loop.

    Values:
        AUTO_BUILDABLE: Ticket can be fully executed by an agent without
            human intervention.
        NEEDS_ARCH_DECISION: Ticket requires architectural decisions or
            design review before implementation can proceed.
        BLOCKED: Ticket has explicit blockers.
        SKIP: Ticket should be skipped in this cycle.
    """

    AUTO_BUILDABLE = "auto_buildable"
    NEEDS_ARCH_DECISION = "needs_arch_decision"
    BLOCKED = "blocked"
    SKIP = "skip"


__all__: list[str] = ["EnumBuildability"]
