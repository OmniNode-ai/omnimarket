# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Batching decision for the dev roll: roll now at the newest ref, or hold.

Pure: the clock is ``facts.observed_at``, every bound is a policy (overlay)
value, nothing is read or sent. Shadow only: a roll decision carries
``would_request`` and no command is published to the deploy agent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.events.dev_roll import (
    EnumDevRollAction,
    EnumDevRollEventRefusal,
    EnumDevRollHealth,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollEventRefused,
    ModelDevRollFacts,
    ModelDevRollRequest,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollRefusalReason,
    ModelDevRollPolicy,
    ModelDevRollRefused,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import load_dev_roll_policy

Result = ModelDevRollDecided | ModelDevRollRefused


def _newest_sha_by_repository(events: list[ModelDevTipChanged]) -> dict[str, str]:
    newest: dict[str, ModelDevTipChanged] = {}
    for event in events:
        held = newest.get(event.repository)
        if held is None or (event.merged_at, event.sha) > (held.merged_at, held.sha):
            newest[event.repository] = event
    return {repo: newest[repo].sha for repo in sorted(newest)}


def decide_dev_roll(facts: ModelDevRollFacts, policy: ModelDevRollPolicy) -> Result:
    """Decide one tick. Deterministic in any input order."""
    now = facts.observed_at
    last = facts.last_roll
    if last is not None:
        for name, moment in (
            ("healthy_since", last.healthy_since),
            ("runtime_started_at", last.runtime_started_at),
        ):
            if moment is not None and moment > now:
                return ModelDevRollRefused(
                    correlation_id=facts.correlation_id,
                    reason=EnumDevRollRefusalReason.INCONSISTENT_LAST_ROLL,
                    detail=f"last roll {last.job_id}: {name} is after observed_at",
                )

    valid: list[ModelDevTipChanged] = []
    refused: list[ModelDevRollEventRefused] = []
    for event in facts.pending_events:
        reason: EnumDevRollEventRefusal | None = None
        if event.repository not in policy.known_repositories:
            reason = EnumDevRollEventRefusal.UNKNOWN_REPOSITORY
        elif event.merged_at > now:
            reason = EnumDevRollEventRefusal.EVENT_AFTER_OBSERVED_AT
        if reason is None:
            valid.append(event)
        else:
            refused.append(
                ModelDevRollEventRefused(
                    event_id=event.event_id, repository=event.repository, reason=reason
                )
            )
    refused.sort(key=lambda r: (str(r.event_id), r.reason.value))
    valid.sort(key=lambda e: (e.merged_at, str(e.event_id)))

    oldest = valid[0].merged_at if valid else None
    wait = int((now - oldest).total_seconds()) if oldest is not None else 0
    newest = _newest_sha_by_repository(valid)

    def hold(
        reason: EnumDevRollHoldReason, until: datetime | None
    ) -> ModelDevRollDecided:
        return ModelDevRollDecided(
            correlation_id=facts.correlation_id,
            observed_at=now,
            action=EnumDevRollAction.HOLD,
            hold_reason=reason,
            hold_until=until,
            shadow=True,
            would_request=None,
            rolled_event_ids=(),
            newest_sha_by_repository=newest,
            oldest_pending_merged_at=oldest,
            longest_wait_seconds=wait,
            refused_events=tuple(refused),
            released_failed_roll_job_id=None,
        )

    if oldest is None:
        return hold(EnumDevRollHoldReason.NO_PENDING_EVENTS, None)
    if last is not None and last.finished_at is None:
        return hold(EnumDevRollHoldReason.ROLL_IN_PROGRESS, None)
    window_end = oldest + timedelta(seconds=policy.batch_window_seconds)
    if now < window_end:
        return hold(EnumDevRollHoldReason.WINDOW_OPEN, window_end)
    if last is not None and last.runtime_started_at is not None:
        interval_end = last.runtime_started_at + timedelta(
            seconds=policy.min_start_interval_seconds
        )
        if now < interval_end:
            return hold(EnumDevRollHoldReason.MIN_START_INTERVAL, interval_end)
    released_failed_roll_job_id: str | None = None
    if last is not None:
        if last.health is not EnumDevRollHealth.HEALTHY or last.healthy_since is None:
            release_at = (
                None
                if last.finished_at is None
                else last.finished_at
                + timedelta(seconds=policy.failed_roll_release_after_seconds)
            )
            if release_at is None or now < release_at:
                return hold(EnumDevRollHoldReason.PREVIOUS_ROLL_NOT_HEALTHY, release_at)
            released_failed_roll_job_id = last.job_id
        else:
            healthy_end = last.healthy_since + timedelta(
                seconds=policy.healthy_for_seconds
            )
            if now < healthy_end:
                return hold(
                    EnumDevRollHoldReason.PREVIOUS_ROLL_NOT_HEALTHY, healthy_end
                )

    git_ref = newest.get(policy.build_context_repository, policy.fallback_git_ref)
    return ModelDevRollDecided(
        correlation_id=facts.correlation_id,
        observed_at=now,
        action=EnumDevRollAction.ROLL_NOW,
        hold_reason=None,
        hold_until=None,
        shadow=True,
        would_request=ModelDevRollRequest(
            correlation_id=facts.correlation_id,
            requested_by=policy.requested_by,
            scope=policy.scope,
            runtime_lane=policy.runtime_lane,
            git_ref=git_ref,
            requested_at=now,
        ),
        rolled_event_ids=tuple(event.event_id for event in valid),
        newest_sha_by_repository=newest,
        oldest_pending_merged_at=oldest,
        longest_wait_seconds=wait,
        refused_events=tuple(refused),
        released_failed_roll_job_id=released_failed_roll_job_id,
    )


class HandlerDevRollRequest:
    """``handle(ModelDevRollFacts) -> ModelDevRollDecided | ModelDevRollRefused``.

    The runtime builds the handler with no arguments; the policy is then read from the
    node's overlay on the first request (no packaged fallback: an absent overlay raises).
    A test passes a policy in.
    """

    def __init__(self, policy: ModelDevRollPolicy | None = None) -> None:
        self._policy = policy

    async def handle(self, request: ModelDevRollFacts) -> Result:
        if self._policy is None:
            self._policy = load_dev_roll_policy()
        return decide_dev_roll(request, self._policy)
