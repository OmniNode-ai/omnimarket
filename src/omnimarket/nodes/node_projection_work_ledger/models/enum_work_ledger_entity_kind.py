# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Entity kinds and state operations of the work-ledger projection (OMN-19513)."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkLedgerEntityKind(StrEnum):
    """What a ``work_ledger_state`` row is an entity of."""

    CLAIM = "claim"
    HOLD = "hold"
    MSG = "msg"
    RULING = "ruling"
    CONSENT = "consent"


class EnumWorkLedgerStateOp(StrEnum):
    """The two column groups a row can write: the opening facts or the closing fact."""

    OPEN = "open"
    CLOSE = "close"


__all__: list[str] = ["EnumWorkLedgerEntityKind", "EnumWorkLedgerStateOp"]
