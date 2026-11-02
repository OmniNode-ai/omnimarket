# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge which candidate hosts take a unit that opens a pull request, from each host's ``scm-pr-create`` probe reading.

``missing`` refuses the host with a typed reason. ``unknown`` (and a host with no PARITY-CAP line) stays admitted when no candidate
reads ``ok``, as before: it widens nothing. When any candidate reads ``ok`` an unread host is refused instead, so a probe that could
not read never counts as a pass beside a host that could. Host names arrive in the request from the declared host table; none is
spelled here.
"""

from __future__ import annotations

from ..models.model_host_capability import ModelHostCapability
from ..models.model_pr_create_placement import (
    EnumPrCreateRefusal,
    ModelHostReading,
    ModelPrCreateHostVerdict,
    ModelPrCreatePlacementRequest,
    ModelPrCreatePlacementResult,
)
from .handler_host_capability import OWNER_FIX, SCM_PR_CREATE

UNREAD = "unread"


def _pr_create_reading(reading: ModelHostReading) -> ModelHostCapability | None:
    return next((c for c in reading.capabilities if c.name == SCM_PR_CREATE), None)


def _verdict(cap: ModelHostCapability | None) -> str:
    return UNREAD if cap is None else cap.verdict


class HandlerPrCreatePlacement:
    def handle(
        self, request: ModelPrCreatePlacementRequest
    ) -> ModelPrCreatePlacementResult:
        if SCM_PR_CREATE not in request.needs:
            return ModelPrCreatePlacementResult(
                verdicts=tuple(
                    ModelPrCreateHostVerdict(
                        host=h.host,
                        admitted=True,
                        reading=_verdict(_pr_create_reading(h)),
                        refusal=None,
                        reason="the unit does not need pull request creation",
                    )
                    for h in request.hosts
                )
            )
        read = {h.host: _pr_create_reading(h) for h in request.hosts}
        peer_ok = any(c is not None and c.verdict == "ok" for c in read.values())
        verdicts: list[ModelPrCreateHostVerdict] = []
        for h in request.hosts:
            cap = read[h.host]
            verdict = _verdict(cap)
            detail = "-" if cap is None else cap.detail
            if verdict == "missing":
                verdicts.append(
                    ModelPrCreateHostVerdict(
                        host=h.host,
                        admitted=False,
                        reading=verdict,
                        refusal=EnumPrCreateRefusal.CAPABILITY_MISSING,
                        reason=f"CAPABILITY-MISSING {SCM_PR_CREATE}: {OWNER_FIX[SCM_PR_CREATE]} ({detail})",
                    )
                )
            elif verdict != "ok" and peer_ok:
                verdicts.append(
                    ModelPrCreateHostVerdict(
                        host=h.host,
                        admitted=False,
                        reading=verdict,
                        refusal=EnumPrCreateRefusal.UNKNOWN_WHILE_PEER_READS_OK,
                        reason=f"{SCM_PR_CREATE} read {verdict} ({detail}) while another candidate host reads ok",
                    )
                )
            else:
                verdicts.append(
                    ModelPrCreateHostVerdict(
                        host=h.host,
                        admitted=True,
                        reading=verdict,
                        refusal=None,
                        reason=f"{SCM_PR_CREATE} read {verdict} ({detail})",
                    )
                )
        return ModelPrCreatePlacementResult(verdicts=tuple(verdicts))
