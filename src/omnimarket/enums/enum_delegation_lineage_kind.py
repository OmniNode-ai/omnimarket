# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one delegation relates to an earlier delegation it follows (OMN-20606)."""

from enum import StrEnum


class EnumDelegationLineageKind(StrEnum):
    """Why a delegation was issued after another one.

    This is a relation between two whole delegations, each with its own
    correlation id. It is not ``EnumDelegationAttemptKind``, which relates
    attempts inside ONE delegation's ladder.

    fallback: the earlier delegation failed and this one asks a different
        route (another lane, an in-process run, another engine) for the same
        work.
    escalation: the earlier delegation failed or was refused and this one asks
        a stronger model on the same route for the same work.
    """

    FALLBACK = "fallback"
    ESCALATION = "escalation"


__all__ = ["EnumDelegationLineageKind"]
