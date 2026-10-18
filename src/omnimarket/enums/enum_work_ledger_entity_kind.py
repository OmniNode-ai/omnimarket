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


class EnumWorkLedgerRelation(StrEnum):
    """How a row relates to an earlier row's entity (OMN-21107, TA.5).

    ``SUPERSEDES`` closes the target; ``AMENDS`` and ``EXTENDS`` link without
    closing; ``CORRECTS`` links a CORRECTION to its target of any kind.
    """

    SUPERSEDES = "supersedes"
    AMENDS = "amends"
    EXTENDS = "extends"
    CORRECTS = "corrects"


__all__: list[str] = [
    "EnumWorkLedgerEntityKind",
    "EnumWorkLedgerRelation",
    "EnumWorkLedgerStateOp",
]
