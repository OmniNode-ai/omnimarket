# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure stall episode judgment: one alert per crossing, one close per resumed thing.

Each watched thing (lane, pull request, scheduled round, host tick) is judged against the budget of the
phase it is in. A thing strictly over budget whose crossing is not an open episode is a new crossing and
yields one alert record for the alarm node. The same thing on the same anchor judged again yields nothing.
A thing that moved, changed phase or, in a complete reading, is gone closes its episode.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.models.model_work_episode import (
    ROUND_PHASE,
    EnumEpisodeClosed,
    EnumWatchedKind,
    ModelOpenEpisode,
    ModelWatchedThing,
    ModelWorkCrossing,
    ModelWorkEpisodeRequest,
    ModelWorkEpisodeResult,
    ModelWorkRecovery,
    crossing_id,
)

_LABEL = {
    EnumWatchedKind.LANE: "Lane",
    EnumWatchedKind.PR: "Pull request",
    EnumWatchedKind.ROUND: "Scheduled round",
    EnumWatchedKind.TICK: "Host tick",
}


def round_things(request: ModelWorkEpisodeRequest) -> tuple[ModelWatchedThing, ...]:
    """Fold the scheduled round heartbeats into one watched thing per job: the newest heartbeat is its anchor."""
    newest: dict[str, tuple[datetime, str, str]] = {}
    for beat in request.round_heartbeats:
        held = newest.get(beat.job_id)
        if held is None or beat.fired_at > held[0]:
            newest[beat.job_id] = (beat.fired_at, beat.tick_id, beat.node_name)
    return tuple(
        ModelWatchedThing(
            kind=EnumWatchedKind.ROUND,
            key=job_id,
            phase=ROUND_PHASE,
            anchor_at=fired_at,
            anchor_id=tick_id,
            owner=node_name,
        )
        for job_id, (fired_at, tick_id, node_name) in sorted(newest.items())
    )


def _budget(request: ModelWorkEpisodeRequest, thing: ModelWatchedThing) -> int:
    if thing.kind is EnumWatchedKind.ROUND and thing.key in request.round_job_budgets:
        return int(request.round_job_budgets[thing.key])
    return int(request.budgets[thing.kind.value][thing.phase])


def _minutes(seconds: int) -> int:
    return seconds // 60


class HandlerWorkEpisodes:
    """Judge watched things against their phase budgets, with the open episodes the caller carried."""

    def handle(self, request: ModelWorkEpisodeRequest) -> ModelWorkEpisodeResult:
        things = request.things + round_things(request)
        by_ref = {(t.kind, t.key): t for t in things}
        open_ids = {episode.crossing_id for episode in request.open_episodes}

        recoveries: list[ModelWorkRecovery] = []
        still_open: list[ModelOpenEpisode] = []
        for episode in request.open_episodes:
            thing = by_ref.get((episode.kind, episode.key))
            reason: EnumEpisodeClosed | None
            if thing is None:
                reason = (
                    EnumEpisodeClosed.GONE
                    if episode.kind in request.complete_kinds
                    else None
                )
            elif thing.phase != episode.phase:
                reason = EnumEpisodeClosed.PHASE_CHANGED
            elif thing.anchor_id != episode.anchor_id:
                reason = EnumEpisodeClosed.MOVED
            else:
                reason = None
            if reason is None:
                still_open.append(episode)
                continue
            recoveries.append(
                ModelWorkRecovery(
                    crossing_id=episode.crossing_id,
                    kind=episode.kind,
                    key=episode.key,
                    phase=episode.phase,
                    reason=reason,
                    text=(
                        f"{_LABEL[episode.kind]} {episode.key} is no longer stalled in "
                        f"{episode.phase} ({reason.value})."
                    ),
                )
            )

        crossings: list[ModelWorkCrossing] = []
        for thing in things:
            budget = _budget(request, thing)
            elapsed = (request.now - thing.anchor_at).total_seconds()
            if elapsed <= budget:
                continue
            ident = crossing_id(thing.kind, thing.key, thing.phase, thing.anchor_id)
            if ident in open_ids:
                continue
            spent = int(elapsed)
            crossings.append(
                ModelWorkCrossing(
                    crossing_id=ident,
                    kind=thing.kind,
                    key=thing.key,
                    phase=thing.phase,
                    anchor_id=thing.anchor_id,
                    budget_seconds=budget,
                    spent_seconds=spent,
                    owner=thing.owner,
                    text=(
                        f"{_LABEL[thing.kind]} {thing.key} has been in {thing.phase} for "
                        f"{_minutes(spent)} minutes against a {_minutes(budget)} minute "
                        f"budget. Owner: {thing.owner}."
                    ),
                )
            )

        opened = [
            ModelOpenEpisode(
                crossing_id=c.crossing_id,
                kind=c.kind,
                key=c.key,
                phase=c.phase,
                anchor_id=c.anchor_id,
                crossed_at=request.now,
                owner=c.owner,
            )
            for c in crossings
        ]
        carried = tuple(still_open) + tuple(opened)
        return ModelWorkEpisodeResult(
            crossings=tuple(crossings),
            recoveries=tuple(recoveries),
            still_open=tuple(still_open),
            open_episodes=carried,
            alarm_request=_alarm_request(request, crossings, recoveries, carried),
        )


def _alarm_request(
    request: ModelWorkEpisodeRequest,
    crossings: list[ModelWorkCrossing],
    recoveries: list[ModelWorkRecovery],
    carried: tuple[ModelOpenEpisode, ...],
) -> ModelLabAlarmDeliveryRequest | None:
    if crossings:
        return ModelLabAlarmDeliveryRequest(
            correlation_id=request.correlation_id,
            condition=request.alarm_condition,
            outcome=EnumDeliveryCondition.ALARM,
            alarms=tuple(
                ModelLabAlarm(
                    condition=request.alarm_condition,
                    subject=c.crossing_id,
                    detail=c.text,
                )
                for c in crossings
            ),
            evidence=(
                f"{len(crossings)} new crossing(s) at {request.now.isoformat()}; "
                f"{len(carried)} episode(s) open"
            ),
        )
    if recoveries and not carried:
        return ModelLabAlarmDeliveryRequest(
            correlation_id=request.correlation_id,
            condition=request.alarm_condition,
            outcome=EnumDeliveryCondition.OK,
            evidence=f"{len(recoveries)} episode(s) closed at {request.now.isoformat()}",
        )
    return None


__all__ = ["HandlerWorkEpisodes", "round_things"]
