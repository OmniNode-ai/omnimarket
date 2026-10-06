# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The states of the PR handoff workflow. Each value equals a ``state_name`` of node_pr_handoff_orchestrator's contract."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffState(StrEnum):
    """The states of the PR handoff workflow. Each value equals a ``state_name`` of node_pr_handoff_orchestrator's contract."""

    REQUESTED = "REQUESTED"
    """A request was read; nothing decided yet."""

    WAITING = "WAITING"
    """Accepted; waiting for the live PR state to be ready."""

    APPENDING = "APPENDING"
    """Ready; the handoff rows are with the ledger effect."""

    HANDED_OFF = "HANDED_OFF"
    """Terminal: the ledger holds the MSG and the closing row."""

    REFUSED = "REFUSED"
    """Terminal error: a typed refusal; nothing was appended."""

    TIMED_OUT = "TIMED_OUT"
    """Terminal error: the wait or the append ran out of budget."""

    @property
    def is_terminal(self) -> bool:
        """HANDED_OFF, REFUSED and TIMED_OUT end the workflow."""
        return self in (
            EnumPrHandoffState.HANDED_OFF,
            EnumPrHandoffState.REFUSED,
            EnumPrHandoffState.TIMED_OUT,
        )


__all__: list[str] = ["EnumPrHandoffState"]
