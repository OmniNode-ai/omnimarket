# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ten states of the PR landing workflow (frozen for wave 1)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingState(StrEnum):
    """One PR's position in the landing workflow.

    Each value equals the ``state_name`` the orchestrator contract's
    ``state_machine`` block declares, so the enum and the contract can be
    compared member for member.
    """

    OBSERVED = "OBSERVED"
    PARKED = "PARKED"
    COMPANION_PENDING = "COMPANION_PENDING"
    COMPANION_OPEN = "COMPANION_OPEN"
    CHECKS_PENDING = "CHECKS_PENDING"
    READY = "READY"
    ARMED = "ARMED"
    NEEDS_AGENT = "NEEDS_AGENT"
    MERGED = "MERGED"
    CLOSED = "CLOSED"

    @property
    def is_terminal(self) -> bool:
        """MERGED and CLOSED end the workflow; every other state can still move."""
        return self in (EnumPrLandingState.MERGED, EnumPrLandingState.CLOSED)


__all__: list[str] = ["EnumPrLandingState"]
