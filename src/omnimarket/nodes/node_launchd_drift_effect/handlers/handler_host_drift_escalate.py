# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure escalation rule: an open item past four bounds, or the cap, escalates (OMN-20853)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from ..models._payload import iso_z
from ..models.model_host_drift_escalate_item import ModelHostDriftEscalateItem
from ..models.model_host_drift_escalate_request import (
    ESCALATION_BOUND_MULTIPLIER,
    ModelHostDriftEscalateRequest,
)
from ..models.model_host_drift_escalate_result import ModelHostDriftEscalateResult
from ..models.model_host_drift_escalation import ModelHostDriftEscalation

# Run staleness belongs to the watchdog (OMN-20812): the heartbeat gap and the check's own unit.
_RUN_STALENESS_KINDS = frozenset({"heartbeat"})
_RUN_STALENESS_KEYS = frozenset({"unit:onex-host-drift", "unit:ai.omninode.host-drift"})


def is_run_staleness(item: ModelHostDriftEscalateItem) -> bool:
    return item.kind in _RUN_STALENESS_KINDS or item.key in _RUN_STALENESS_KEYS


def escalate_after_seconds(bound_seconds: int, cap_seconds: int) -> int:
    """Four times the bound, capped; a zero bound has no multiple, so only the cap applies."""
    if bound_seconds <= 0:
        return cap_seconds
    return min(ESCALATION_BOUND_MULTIPLIER * bound_seconds, cap_seconds)


class HandlerHostDriftEscalate:
    def handle(
        self, request: ModelHostDriftEscalateRequest
    ) -> ModelHostDriftEscalateResult:
        escalations: list[ModelHostDriftEscalation] = []
        for item in request.items:
            if is_run_staleness(item):
                continue
            after = escalate_after_seconds(item.bound_seconds, request.cap_seconds)
            age = int((request.now - item.first_seen_at).total_seconds())
            if age < after:
                continue
            escalations.append(
                ModelHostDriftEscalation(
                    episode_id=hashlib.sha256(
                        f"{item.host}|{item.key}|{iso_z(item.first_seen_at)}".encode()
                    ).hexdigest()[:16],
                    host=item.host,
                    key=item.key,
                    kind=item.kind,
                    verdict=item.verdict,
                    observed=item.observed,
                    first_seen_at=item.first_seen_at,
                    escalated_at=item.first_seen_at + timedelta(seconds=after),
                    escalate_after_seconds=after,
                    age_seconds=age,
                    apply_class=item.apply_class,
                    command=item.command,
                    fix_attempts=item.fix_attempts,
                    fix_outcome=item.fix_outcome,
                    fix_detail=item.fix_detail,
                )
            )
        return ModelHostDriftEscalateResult(
            tuple(sorted(escalations, key=lambda e: (e.host, e.key, e.first_seen_at)))
        )
