# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostHygieneDecision: per (host, cleanup) due decisions, as a pure function.

Definition-B compute: ``handle(request: ModelHostHygieneDecisionRequest) -> ModelHostHygieneDecision``.
No clock (``now`` is an input), no I/O, no hard-coded threshold.
"""

from __future__ import annotations

from omnimarket.models.model_host_hygiene import (
    EnumHostHygieneReason,
    EnumHostHygieneUnreadPolicy,
    ModelHostHygieneCleanupPolicy,
    ModelHostHygieneDecision,
    ModelHostHygieneDecisionItem,
    ModelHostHygieneDecisionRequest,
)


def decide_host_hygiene(
    request: ModelHostHygieneDecisionRequest,
) -> ModelHostHygieneDecision:
    """Decide, per (host, cleanup), whether the cleanup is due."""
    policy_by_id = {c.cleanup_id: c for c in request.cleanups}
    items: list[ModelHostHygieneDecisionItem] = []
    for host_facts in request.hosts:
        reading = host_facts.reading
        used_fraction = reading.used_fraction if reading is not None else None
        age = (
            int((request.now - reading.read_at).total_seconds())
            if reading is not None
            else None
        )
        fresh = (
            used_fraction is not None
            and age is not None
            and 0 <= age <= request.max_reading_age_seconds
        )
        last_dispatch = {
            ld.cleanup_id: ld.dispatched_at for ld in host_facts.last_dispatched
        }
        for cleanup_id in host_facts.cleanup_ids:
            policy = policy_by_id[cleanup_id]
            due, reason = _decide_one(
                policy,
                fresh=fresh,
                used_fraction=used_fraction,
                since_dispatch=(
                    (request.now - last_dispatch[cleanup_id]).total_seconds()
                    if cleanup_id in last_dispatch
                    else None
                ),
            )
            items.append(
                ModelHostHygieneDecisionItem(
                    host=host_facts.host,
                    cleanup_id=cleanup_id,
                    due=due,
                    reason=reason,
                    used_fraction=used_fraction,
                    reading_age_seconds=age,
                )
            )
    return ModelHostHygieneDecision(decided_at=request.now, items=tuple(items))


def _decide_one(
    policy: ModelHostHygieneCleanupPolicy,
    *,
    fresh: bool,
    used_fraction: float | None,
    since_dispatch: float | None,
) -> tuple[bool, EnumHostHygieneReason]:
    if since_dispatch is not None and since_dispatch < policy.min_interval_seconds:
        return False, EnumHostHygieneReason.INTERVAL_NOT_ELAPSED
    if fresh and used_fraction is not None:
        if used_fraction >= policy.min_used_fraction:
            return True, EnumHostHygieneReason.DISK_PRESSURE
        return False, EnumHostHygieneReason.BELOW_THRESHOLD
    if policy.when_unread == EnumHostHygieneUnreadPolicy.DISPATCH:
        return True, EnumHostHygieneReason.UNREAD_RUN_ANYWAY
    return False, EnumHostHygieneReason.UNREAD_SKIPPED


class HandlerHostHygieneDecision:
    """ONEX handler for the host hygiene decision compute."""

    def handle(
        self, request: ModelHostHygieneDecisionRequest
    ) -> ModelHostHygieneDecision:
        return decide_host_hygiene(request)
