# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger-projection-stale condition: the projection is not behind the ledger file by more than the bound.

The old lab alarm's work-ledger projection condition, ported with its rule unchanged: the newest canonical row of the
rolling ledger file against the newest row of the projection. A projection that trails the file by more than the
overlay's bound, or holds no row in the overlay's window, is a breach. A subject alarms only after its streak of
consecutive breaching runs reaches the overlay's persistence bound; before that the run is OK with a waiting
reason. An unreadable file or projection is INDETERMINATE, never ALARM and never OK. It measures file-versus-projection
freshness only, never the liveness of the emit path or of the projection writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_work_ledger_projection_stale import (
    EnumWorkLedgerProjectionStaleFault,
    EnumWorkLedgerProjectionStaleOutcome,
    ModelWorkLedgerProjectionStaleCommand,
    ModelWorkLedgerProjectionStaleResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelWorkLedgerProjectionStaleSettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    LedgerUnreadableError,
    ProjectionUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolGrowthStreakStore,
    ProtocolLedgerNewestReader,
    ProtocolProjectionNewestReader,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "work_ledger_projection_stale"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerWorkLedgerProjectionStale:
    """Compare the ledger file's newest row with the projection's; the subject alarms when its streak reaches the bound."""

    def __init__(
        self,
        *,
        settings: ModelWorkLedgerProjectionStaleSettings,
        ledger: ProtocolLedgerNewestReader,
        projection: ProtocolProjectionNewestReader,
        streaks: ProtocolGrowthStreakStore,
        delivery: ProtocolAlarmDelivery | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._projection = projection
        self._streaks = streaks
        self._delivery = delivery
        self._clock = clock

    def handle(
        self, request: ModelWorkLedgerProjectionStaleCommand
    ) -> ModelWorkLedgerProjectionStaleResult:
        settings = self._settings
        subject = f"work-ledger-projection:{settings.projection_name}"
        if request.fault is EnumWorkLedgerProjectionStaleFault.PROJECTION_BEHIND:
            detail = (
                f"{settings.projection_name} is (injected) behind the ledger file, past its "
                f"declared bound of {settings.max_lag_seconds:g}s"
            )
            return self._finish(
                request,
                EnumWorkLedgerProjectionStaleOutcome.ALARM,
                subject,
                (),
                (f"{subject}: {detail}",),
                ("injected: projection-behind",),
                detail,
            )
        try:
            file_newest, row_type = self._ledger.newest()
        except LedgerUnreadableError as exc:
            return self._finish(
                request,
                EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE,
                subject,
                (),
                (),
                ("ledger-unreadable",),
                f"the ledger file could not be read, so undated is not current: {exc}",
            )
        since = file_newest - timedelta(seconds=settings.projection_window_seconds)
        try:
            projection_newest = self._projection.newest(since)
        except ProjectionUnreadableError as exc:
            return self._finish(
                request,
                EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE,
                subject,
                (f"ledger newest {row_type} at {file_newest.isoformat()}",),
                (),
                ("projection-unreadable",),
                f"the projection could not be read, so unread is not caught up: {exc}",
            )
        ledger_fact = f"ledger newest {row_type} at {file_newest.isoformat()}"
        breach: tuple[str, str] | None = None
        evidence = ""
        reading = ""
        if projection_newest is None:
            reading = (
                f"{ledger_fact}; the projection holds no row since {since.isoformat()}"
            )
            breach = (
                reading,
                f"the ledger's newest canonical row is {row_type} at {file_newest.isoformat()} and "
                f"{settings.projection_name} holds no row since {since.isoformat()} -- the projection "
                f"is behind the ledger file by at least the {settings.projection_window_seconds:g}s window",
            )
        else:
            lag = (file_newest - projection_newest).total_seconds()
            reading = (
                f"{ledger_fact}, projection newest {projection_newest.isoformat()}, "
                f"lag {lag:.0f}s (bound {settings.max_lag_seconds:g}s)"
            )
            if lag > settings.max_lag_seconds:
                breach = (
                    reading,
                    f"{settings.projection_name} is {lag:.0f}s behind the ledger file, past its declared "
                    f"bound of {settings.max_lag_seconds:g}s: {reading}. Consumer-group lag can read 0 "
                    "through this: nothing new reaching the topics looks caught up from the broker's side",
                )
            else:
                evidence = (
                    f"{reading}; this measures file-versus-projection freshness only and is not a "
                    "liveness statement about the emit path or the projection writer"
                )
        stored = self._streaks.load()
        if breach is None:
            if request.write:
                stored.pop(subject, None)
                self._streaks.save(stored)
            return self._finish(
                request,
                EnumWorkLedgerProjectionStaleOutcome.OK,
                subject,
                (reading,),
                (),
                (),
                evidence,
            )
        breach_reading, detail = breach
        streak = stored.get(subject, 0) + 1
        alarms: list[str] = []
        reasons: list[str] = []
        self._classify(subject, streak, detail, alarms, reasons)
        if request.write:
            stored[subject] = streak
            self._streaks.save(stored)
        outcome = (
            EnumWorkLedgerProjectionStaleOutcome.ALARM
            if alarms
            else EnumWorkLedgerProjectionStaleOutcome.OK
        )
        return self._finish(
            request,
            outcome,
            subject,
            (breach_reading,),
            tuple(alarms),
            tuple(reasons),
            detail,
        )

    def _classify(
        self,
        subject: str,
        streak: int,
        detail: str,
        alarms: list[str],
        reasons: list[str],
    ) -> None:
        bound = self._settings.persistence.persist_runs
        if streak >= bound:
            alarms.append(f"{subject}: {detail}")
        else:
            reasons.append(f"waiting:{subject}: run {streak} of {bound}")

    def _finish(
        self,
        request: ModelWorkLedgerProjectionStaleCommand,
        outcome: EnumWorkLedgerProjectionStaleOutcome,
        subject: str,
        readings: tuple[str, ...],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelWorkLedgerProjectionStaleResult:
        failure = ""
        if outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelWorkLedgerProjectionStaleResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            subject=subject,
            readings=readings,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._deliver(request, result)

    def _deliver(
        self,
        request: ModelWorkLedgerProjectionStaleCommand,
        result: ModelWorkLedgerProjectionStaleResult,
    ) -> ModelWorkLedgerProjectionStaleResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumWorkLedgerProjectionStaleFault.NONE
        ):
            return result
        try:
            typed = []
            for line in result.alarms:
                s, sep, d = line.partition(": ")
                if not sep:
                    raise ValueError(
                        f"alarm line {line!r} names no subject before ': '"
                    )
                typed.append(ModelLabAlarm(condition=CONDITION, subject=s, detail=d))
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=CONDITION,
                    outcome=EnumDeliveryCondition(result.outcome.value),
                    alarms=tuple(typed),
                    evidence=result.evidence,
                )
            )
        except (ValueError, OSError) as exc:
            return self._with_delivery_failure(
                result, (), f"delivery-unavailable:{type(exc).__name__}"
            )
        entries = tuple(
            f"{d.key} channel={d.channel.value} ledger={d.ledger.value}"
            for d in reply.deliveries
        )
        if reply.outcome is EnumDeliveryOutcome.FAILED:
            return self._with_delivery_failure(
                result, entries, f"delivery-failed:{reply.failure}"
            )
        return result.model_copy(update={"delivery": entries})

    @staticmethod
    def _with_delivery_failure(
        result: ModelWorkLedgerProjectionStaleResult,
        entries: tuple[str, ...],
        failure: str,
    ) -> ModelWorkLedgerProjectionStaleResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelWorkLedgerProjectionStaleResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["work_ledger_projection_stale_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "work_ledger_projection_stale_run_completed"
        if result.outcome is EnumWorkLedgerProjectionStaleOutcome.OK
        else "work_ledger_projection_stale_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "subject": result.subject,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerWorkLedgerProjectionStale",
    "events_for",
    "load_topics",
]
