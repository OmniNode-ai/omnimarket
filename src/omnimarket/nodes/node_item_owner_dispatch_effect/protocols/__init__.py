# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_item_owner_dispatch_effect: the ledger rows, the plan owner check, the reservation and the appender.

The brief preparer and the lane runner are the host drift owner dispatch's own ports, so an owner lane is checked and
placed by the same path.
"""

from __future__ import annotations

from typing import Protocol


class LedgerUnreadableError(Exception):
    """The rows could not be read from the ledger; the message names why."""


class PlanOwnerUnreadableError(Exception):
    """The plan owner check gave no fresh answer; the message names why."""


class RowAppendFailedError(Exception):
    """The ledger did not accept a row; the message names why."""


class ProtocolLedgerRows(Protocol):
    def read(self) -> tuple[str, ...]:
        """The ledger rows of the lookback window, in any order; raises LedgerUnreadableError."""
        ...


class ProtocolPlanOwnerMissing(Protocol):
    def read(self) -> tuple[str, ...]:
        """The rules the plan owner check answered MISSING; raises PlanOwnerUnreadableError."""
        ...


class ProtocolItemReservation(Protocol):
    def reserve(self, key: str) -> bool:
        """True for the first caller of ``key`` (item, revision, generation), false for every later or racing one.

        On the Temporal path this is the engine's workflow-id uniqueness: the owner step for one key is a child
        workflow whose id is the key, so a second start of it, from a rerun, a second tick or a second worker, fails.
        """
        ...


class ProtocolRowAppender(Protocol):
    def append(self, row: str) -> None:
        """Append one row to the rolling ledger; raises RowAppendFailedError."""
        ...


__all__ = [
    "LedgerUnreadableError",
    "PlanOwnerUnreadableError",
    "ProtocolItemReservation",
    "ProtocolLedgerRows",
    "ProtocolPlanOwnerMissing",
    "ProtocolRowAppender",
    "RowAppendFailedError",
]
