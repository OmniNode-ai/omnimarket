# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dead-man's decisions (OMN-20807, plan 4.8 and 4.9).

Three things, all from the facts of one tick and ``now``:

* PRIMARY_SILENT -- the primary watchdog's heartbeat is missing or older than
  its bound. With the broker unreachable too, the alarm also names the bus
  (BUS_SILENT, seen from this host) and the route is direct: the alert channel
  is reached without the bus.
* One ledger row per opened and closed alarm episode, oldest event first and
  stamped with the episode's own time, so episodes opened during a bus outage
  are recorded in order when the bus returns. A key the store already holds is
  never written again.
* The daily canary: a judged day whose episode was not delivered raises
  ALARM_PATH_BROKEN (CANARY_UNDELIVERED), and one delivered but not recorded
  raises it (CANARY_UNRECORDED).

An alarm's episode id is derived from what it is about, so evaluating the same
silence twice names the same episode and the outbox keeps one row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.models.liveness.model_automation_liveness import (
    VERDICT_STATE,
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmRaised,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    CANARY_PROCESS_ID,
    EnumDeadmanDeliveryRoute,
    EnumDeadmanPhase,
    ModelAutomationLivenessDeadmanRequest,
    ModelAutomationLivenessDeadmanResult,
    ModelDeadmanCanaryDay,
    ModelDeadmanEpisodeKey,
    ModelDeadmanLedgerRow,
)

PRIMARY_PROCESS_ID = "watchdog/primary"
BUS_PROCESS_ID = "bus/broker"
LEDGER_LANE = "automation-liveness"

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason
_NAMESPACE = uuid5(NAMESPACE_URL, "omnimarket:automation-liveness-deadman")
_PHASE_ORDER = {EnumDeadmanPhase.OPENED: 0, EnumDeadmanPhase.CLOSED: 1}


def _episode(kind: str, about: str) -> UUID:
    return uuid5(_NAMESPACE, f"{kind}:{about}")


def _clean(text: str) -> str:
    """One ledger cell: no cell separator and no line break."""
    return " ".join(text.replace("|", "/").split())


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerAutomationLivenessDeadman:
    def handle(
        self, request: ModelAutomationLivenessDeadmanRequest
    ) -> ModelAutomationLivenessDeadmanResult:
        silent_since = self._primary_silent_since(request)
        alarms: list[ModelAutomationAlarmRaised] = []
        if silent_since is not None:
            alarms.append(self._primary_silent(request, silent_since))
            if not request.broker_reachable:
                alarms.append(self._bus_silent(request, silent_since))
        canary = tuple(self._canary_day(request, day) for day in request.canary_dates)
        alarms.extend(
            self._path_broken(request, day) for day in canary if self._is_broken(day)
        )
        return ModelAutomationLivenessDeadmanResult(
            now=request.now,
            delivery_route=(
                EnumDeadmanDeliveryRoute.BUS
                if request.broker_reachable
                else EnumDeadmanDeliveryRoute.DIRECT
            ),
            alarms_raised=tuple(alarms),
            ledger_rows=self._ledger_rows(request),
            canary=canary,
        )

    # ------------------------------------------------------------- primary and bus

    @staticmethod
    def _primary_silent_since(
        request: ModelAutomationLivenessDeadmanRequest,
    ) -> datetime | None:
        """When the primary became silent, or None while its heartbeat is fresh."""
        last = request.primary_last_heartbeat_at
        bound = timedelta(seconds=request.primary_heartbeat_bound_seconds)
        if last is None:
            return request.now
        if request.now - last > bound:
            return last + bound
        return None

    @staticmethod
    def _heard(request: ModelAutomationLivenessDeadmanRequest) -> str:
        last = request.primary_last_heartbeat_at
        return "never" if last is None else last.isoformat()

    def _raise(
        self,
        request: ModelAutomationLivenessDeadmanRequest,
        *,
        about: str,
        process_id: str,
        host: str,
        verdict: _V,
        reason: _R,
        opened_at: datetime,
        evidence: str,
        action: str,
    ) -> ModelAutomationAlarmRaised:
        return ModelAutomationAlarmRaised(
            episode_id=_episode(verdict.value, about),
            process_id=process_id,
            host=host,
            verdict=verdict,
            state=VERDICT_STATE[verdict],
            reason=reason,
            severity=EnumAlarmSeverity.HIGH,
            opened_at=opened_at,
            delivery_due_at=max(opened_at, request.now)
            + timedelta(seconds=request.delivery_budget_seconds),
            evidence_ref=evidence,
            action=action,
        )

    def _primary_silent(
        self, request: ModelAutomationLivenessDeadmanRequest, since: datetime
    ) -> ModelAutomationAlarmRaised:
        return self._raise(
            request,
            about=self._heard(request),
            process_id=PRIMARY_PROCESS_ID,
            host=request.primary_host,
            verdict=_V.PRIMARY_SILENT,
            reason=_R.PRIMARY_HEARTBEAT_MISSED,
            opened_at=since,
            evidence=f"deadman:primary-heartbeat={self._heard(request)}",
            action=f"reinstall:{PRIMARY_PROCESS_ID}",
        )

    def _bus_silent(
        self, request: ModelAutomationLivenessDeadmanRequest, since: datetime
    ) -> ModelAutomationAlarmRaised:
        return self._raise(
            request,
            about=self._heard(request),
            process_id=BUS_PROCESS_ID,
            host=request.primary_host,
            verdict=_V.BUS_SILENT,
            reason=_R.NO_EVENT_AND_BROKER_UNANSWERED,
            opened_at=since,
            evidence="deadman:broker-unreachable-with-primary-silent",
            action=f"diagnose:{BUS_PROCESS_ID}",
        )

    # --------------------------------------------------------------------- canary

    @staticmethod
    def _canary_day(
        request: ModelAutomationLivenessDeadmanRequest, day: date
    ) -> ModelDeadmanCanaryDay:
        injection = datetime.combine(
            day, time(request.canary_injection_hour_utc), tzinfo=UTC
        )
        judged = request.now > injection + timedelta(
            seconds=request.canary_receipt_budget_seconds
        )
        opened = sorted(
            (
                e
                for e in request.opened
                if e.process_id == CANARY_PROCESS_ID
                and injection <= e.opened_at < injection + timedelta(days=1)
            ),
            key=lambda e: e.opened_at,
        )
        if not opened:
            return ModelDeadmanCanaryDay(day=day, judged=judged)
        episode_id = opened[0].episode_id
        return ModelDeadmanCanaryDay(
            day=day,
            judged=judged,
            episode_id=episode_id,
            opened=True,
            delivered=any(
                d.episode_id == episode_id and d.delivered for d in request.delivered
            ),
            recorded=any(r.episode_id == episode_id for r in request.recorded),
            closed=any(c.episode_id == episode_id for c in request.closed),
        )

    @staticmethod
    def _is_broken(day: ModelDeadmanCanaryDay) -> bool:
        return day.judged and not (day.opened and day.delivered and day.recorded)

    def _path_broken(
        self, request: ModelAutomationLivenessDeadmanRequest, day: ModelDeadmanCanaryDay
    ) -> ModelAutomationAlarmRaised:
        injection = datetime.combine(
            day.day, time(request.canary_injection_hour_utc), tzinfo=UTC
        )
        undelivered = not (day.opened and day.delivered)
        return self._raise(
            request,
            about=day.day.isoformat(),
            process_id=CANARY_PROCESS_ID,
            host=request.canary_host,
            verdict=_V.ALARM_PATH_BROKEN,
            reason=_R.CANARY_UNDELIVERED if undelivered else _R.CANARY_UNRECORDED,
            opened_at=injection
            + timedelta(seconds=request.canary_receipt_budget_seconds),
            evidence=(
                f"deadman:canary-{day.day.isoformat()}"
                f":opened={day.opened}:delivered={day.delivered}:recorded={day.recorded}"
            ),
            action=f"diagnose:{CANARY_PROCESS_ID}",
        )

    # --------------------------------------------------------------------- ledger

    def _ledger_rows(
        self, request: ModelAutomationLivenessDeadmanRequest
    ) -> tuple[ModelDeadmanLedgerRow, ...]:
        seen = set(request.already_recorded)
        actions = {e.episode_id: e.action for e in request.opened}
        rows: list[ModelDeadmanLedgerRow] = []
        for event in request.opened:
            key = ModelDeadmanEpisodeKey(
                episode_id=event.episode_id, phase=EnumDeadmanPhase.OPENED
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                self._row(
                    request,
                    key=key,
                    at=event.opened_at,
                    process_id=event.process_id,
                    verdict=event.verdict,
                    action=event.action,
                    detail=(
                        f"episode opened: {event.reason.value}, "
                        f"severity {event.severity.value}, evidence {event.evidence_ref}"
                    ),
                )
            )
        for closing in request.closed:
            key = ModelDeadmanEpisodeKey(
                episode_id=closing.episode_id, phase=EnumDeadmanPhase.CLOSED
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                self._row(
                    request,
                    key=key,
                    at=closing.cleared_at,
                    process_id=closing.process_id,
                    verdict=closing.verdict,
                    action=actions.get(
                        closing.episode_id, f"close:{closing.process_id}"
                    ),
                    detail=self._closed_detail(closing),
                )
            )
        rows.sort(
            key=lambda r: (r.event_at, _PHASE_ORDER[r.key.phase], str(r.key.episode_id))
        )
        return tuple(rows)

    @staticmethod
    def _closed_detail(closing: ModelAutomationAlarmCleared) -> str:
        return (
            f"episode closed after hysteresis; opened "
            f"{_stamp(closing.opened_at)}, cleared {_stamp(closing.cleared_at)}"
        )

    @staticmethod
    def _row(
        request: ModelAutomationLivenessDeadmanRequest,
        *,
        key: ModelDeadmanEpisodeKey,
        at: datetime,
        process_id: str,
        verdict: _V,
        action: str,
        detail: str,
    ) -> ModelDeadmanLedgerRow:
        row = (
            f"{_stamp(at)} | FRICTION | lane={LEDGER_LANE} "
            f"| existing={request.ledger_ticket} "
            f"| cost=~0 lane-minutes at record (accrues until the episode closes) "
            f"| class=automation-liveness | process={_clean(process_id)} "
            f"| verdict={verdict.value} | episode={key.episode_id} "
            f"| phase={key.phase.value} | action={_clean(action)} | {_clean(detail)}"
        )
        return ModelDeadmanLedgerRow(
            key=key, event_at=at, process_id=process_id, row=row
        )


__all__ = [
    "BUS_PROCESS_ID",
    "LEDGER_LANE",
    "PRIMARY_PROCESS_ID",
    "HandlerAutomationLivenessDeadman",
]
