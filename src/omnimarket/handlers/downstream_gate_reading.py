# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared reading of a head's downstream check.

``read_head_downstream`` is the pure reading the landing decision asks of the downstream check read for a pull
request into a repository other repositories install. Only a check of the exact head counts: a ``pass`` on the head
passes, a ``break`` names the first failing import, an ``unread`` check (the install or the smoke gave no answer) is
held exactly as a break is, and a check of another head reads ``other_head``. No clock and no I/O.
"""

from __future__ import annotations

from omnimarket.models.model_downstream_gate import (
    EnumDownstreamGateVerdict,
    EnumDownstreamVerdict,
    ModelDownstreamGateFact,
    ModelDownstreamGateReading,
)


def read_head_downstream(
    fact: ModelDownstreamGateFact, head_sha: str
) -> ModelDownstreamGateReading:
    """The reading of one downstream check against the head being merged."""
    if fact.head_sha != head_sha:
        return ModelDownstreamGateReading(
            verdict=EnumDownstreamGateVerdict.OTHER_HEAD,
            detail=(
                f"the downstream check ran on {fact.head_sha[:12]}, not on the head {head_sha[:12]}: "
                "a check of an older head does not count"
            ),
        )
    if fact.verdict is EnumDownstreamVerdict.PASS:
        return ModelDownstreamGateReading(
            verdict=EnumDownstreamGateVerdict.PASS,
            detail=f"every downstream repository passed with the head {head_sha[:12]} installed",
        )
    verdict = (
        EnumDownstreamGateVerdict.BREAK
        if fact.verdict is EnumDownstreamVerdict.BREAK
        else EnumDownstreamGateVerdict.UNREAD
    )
    return ModelDownstreamGateReading(
        verdict=verdict, detail="; ".join(fact.reasons) or fact.verdict.value
    )


__all__ = ["read_head_downstream"]
