# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agree on ownership before considering completion; unread never means free."""

import re
from datetime import UTC

from ..models.model_lab_fill_ownership import (
    ModelLabFillOwnershipRequest,
    ModelLabFillOwnershipResult,
    ModelLabFillOwnershipVerdict,
)
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS
from .helpers_outcomes import by_lane


def _clean(value: str) -> str:
    return re.sub(r"[|\n\r]", "/", value).strip()[:120]


class HandlerLabFillOwnership:
    """Port the verdict loop only; the effect supplies every reader's facts."""

    def handle(
        self, request: ModelLabFillOwnershipRequest
    ) -> ModelLabFillOwnershipResult:
        verdicts = []
        for item in request.lanes:
            if (
                request.verdict_lanes is not None
                and item.lane not in request.verdict_lanes
            ):
                continue
            ticket, pr = item.ticket, item.pr.strip().lower().split("/")[-1]
            readings: list[tuple[str, str, str]] = []
            index = request.claim_index
            if isinstance(index, str):
                readings.append(("claim-index", "unreadable", _clean(index)))
            else:
                record = index.get(ticket)
                if record is None:
                    readings.append(("claim-index", "free", ""))
                elif record.state == "held":
                    readings.append(("claim-index", "owned", record.lane))
                else:
                    readings.append(
                        ("claim-index", "free", _clean("stale:" + record.lane))
                    )
            claims = request.ledger_claims
            if isinstance(claims, str) and claims:
                readings.append(("ledger-claims", "unreadable", _clean(claims)))
            else:
                now = (
                    request.now.replace(tzinfo=UTC)
                    if request.now.tzinfo is None
                    else request.now
                )
                holders = (
                    sorted(
                        {
                            c.lane
                            for c in {(c.lane, c.subject): c for c in claims}.values()
                            if c.subject in (ticket, pr)
                            and (
                                now
                                - (
                                    c.when.replace(tzinfo=UTC)
                                    if c.when.tzinfo is None
                                    else c.when
                                )
                            ).total_seconds()
                            <= request.staleness_hours * 3600
                        }
                    )
                    if not isinstance(claims, str)
                    else []
                )
                readings.append(
                    ("ledger-claims", "owned" if holders else "free", ",".join(holders))
                )
            if pr:
                registry = request.pr_claims
                if isinstance(registry, str) and registry:
                    readings.append(("pr-claims", "unreadable", _clean(registry)))
                elif isinstance(registry, dict) and pr in registry:
                    readings.append(("pr-claims", "owned", registry[pr]))
                else:
                    readings.append(("pr-claims", "free", ""))
            owned = [v for _, s, v in readings if s == "owned"]
            free = any(s == "free" for _, s, _ in readings)
            failed = any(s == "unreadable" for _, s, _ in readings)
            detail = "; ".join(
                r
                + "="
                + (
                    "owned:" + v
                    if s == "owned"
                    else "unreadable:" + v
                    if s == "unreadable"
                    else "free" + ("(" + v + ")" if v else "")
                )
                for r, s, v in readings
            )
            skipped = ""
            if owned:
                skipped = (
                    "owned:" + owned[0]
                    if not free and len(set(owned)) == 1
                    else "owned-ambiguous"
                )
            elif failed:
                skipped = "owned-unreadable"
            elif item.kind in APPROVED_KINDS:
                watcher = request.watcher_merged
                repo = item.repo.split("/")[-1].lower()
                names = (
                    sorted(
                        n
                        for n in set(watcher.get(ticket, ()))
                        if not repo or n.split("#")[0] == repo
                    )
                    if isinstance(watcher, dict)
                    else []
                )
                if isinstance(watcher, str) and watcher:
                    skipped, detail = (
                        "completion-unreadable",
                        detail + "; watcher-merged=unreadable:" + _clean(watcher),
                    )
                elif names:
                    skipped, detail = (
                        "done",
                        detail + "; watcher-merged=done:" + ",".join(names),
                    )
            verdicts.append(
                ModelLabFillOwnershipVerdict(
                    lane=item.lane, skipped=skipped, detail=detail
                )
            )
        by = {v.lane: v for v in verdicts}
        proceed = []
        skipped_entries: list[dict[str, object]] = []
        for item in request.lanes:
            v = by.get(item.lane)
            reason = v.skipped if v else "ownership-unread"
            if reason:
                skipped_entries.append(
                    {
                        "lane": item.lane,
                        "detached": False,
                        "brief_has_delegation": False,
                        "elapsed_s": 0,
                        "skipped": reason,
                        "detail": v.detail if v else "no verdict returned",
                    }
                )
            else:
                proceed.append(item)
        merged = by_lane([*skipped_entries, *request.returned])
        return ModelLabFillOwnershipResult(
            verdicts=tuple(verdicts),
            proceed=tuple(proceed),
            skipped=tuple(skipped_entries),
            dispatched=tuple(
                dict(merged[i.lane]) for i in request.lanes if i.lane in merged
            ),
        )
