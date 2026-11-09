# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The send gate: skip a merged ticket to the closer, and keep an h200-bound brief off a host that cannot reach h200 (OMN-23277)."""

from __future__ import annotations

import re

from ..models.model_lab_fill_send_gate import (
    EnumLabFillSendVerdict,
    ModelLabFillSendGateRequest,
    ModelLabFillSendGateResult,
)


def brief_needs_hosts(
    brief: str, fact_hosts: tuple[str, ...], needs_hosts: tuple[str, ...]
) -> tuple[str, ...]:
    """The fact hosts the brief's own words name, plus the ones the caller declared, in fact-host order."""
    named = {
        h
        for h in fact_hosts
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(h)}(?![A-Za-z0-9_-])", brief)
    }
    return tuple(h for h in fact_hosts if h in named or h in needs_hosts) + tuple(
        h for h in needs_hosts if h not in fact_hosts
    )


def _reaches(
    host: str, needed: tuple[str, ...], reach: dict[str, tuple[str, ...]]
) -> bool:
    return all(n == host or n in reach.get(host, ()) for n in needed)


class HandlerLabFillSendGate:
    """Decide from supplied facts alone; reads nothing and launches nothing."""

    def handle(
        self, request: ModelLabFillSendGateRequest
    ) -> ModelLabFillSendGateResult:
        merged = tuple(sorted(set(request.merged_prs)))
        if merged and request.kind not in request.exempt_kinds:
            # Whether the ticket is done is the closer's question, not a code lane's.
            return ModelLabFillSendGateResult(
                verdict=EnumLabFillSendVerdict.IMPLEMENTATION_MERGED,
                detail=f"implementation merged in {','.join(merged)}",
                routed_to=request.closer,
                merged_prs=merged,
            )
        needed = brief_needs_hosts(
            request.brief, request.fact_hosts, request.needs_hosts
        )
        if not needed:
            return ModelLabFillSendGateResult(verdict=EnumLabFillSendVerdict.SEND)
        reach = {k: tuple(v) for k, v in request.host_reach.items()}
        if request.host:
            if _reaches(request.host, needed, reach):
                return ModelLabFillSendGateResult(
                    verdict=EnumLabFillSendVerdict.SEND, needs_hosts=needed
                )
            return ModelLabFillSendGateResult(
                verdict=EnumLabFillSendVerdict.FACT_HOST_UNREACHABLE,
                detail=f"brief facts need {','.join(needed)}; {request.host} does not reach it",
                needs_hosts=needed,
            )
        # Unpinned: the runner would place it anywhere, so send it only to a host that reaches the facts.
        candidates = sorted({*reach, *needed})
        chosen = next((h for h in candidates if _reaches(h, needed, reach)), None)
        if chosen is None:
            return ModelLabFillSendGateResult(
                verdict=EnumLabFillSendVerdict.FACT_HOST_UNREACHABLE,
                detail=f"brief facts need {','.join(needed)}; no host reaches it",
                needs_hosts=needed,
            )
        return ModelLabFillSendGateResult(
            verdict=EnumLabFillSendVerdict.SEND, host=chosen, needs_hosts=needed
        )
