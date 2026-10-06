# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger effect's answer for one append attempt."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffLedgerStatus(StrEnum):
    """The ledger effect's answer for one append attempt."""

    ACCEPTED = "accepted"
    """The ledger appended the rows."""

    DUPLICATE = "duplicate"
    """The ledger had already appended this request id: the rows are there."""

    REFUSED = "refused"
    """The ledger refused the rows (grammar, or a row type the bus does not accept)."""

    ERROR = "error"
    """The ledger host failed the append."""

    PENDING = "pending"
    """No receipt within the effect's timeout: the rows may or may not be there; resending the same request id is safe."""


__all__: list[str] = ["EnumPrHandoffLedgerStatus"]
