# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation-chain-canary condition: the newest completed run of the protected delegation lane is green and fresh.

The old lab alarm's delegation chain canary, ported with its rule unchanged: the newest completed run of the
workflow on the branch, among the declared events, decides. A run that is red, stale or missing is a breach.
A subject alarms only after its streak of consecutive breaching runs reaches the overlay's persistence bound;
before that the run is OK with a waiting reason. An unreadable source is INDETERMINATE, never ALARM and never
OK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_delegation_chain_canary import (
    EnumDelegationChainCanaryFault,
    EnumDelegationChainCanaryOutcome,
    ModelDelegationChainCanaryCommand,
    ModelDelegationChainCanaryResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelDelegationChainCanarySettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    CanaryRunsUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolCanaryRunSource,
    ProtocolGrowthStreakStore,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "delegation_chain_canary"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerDelegationChainCanary:
    """Read the canary runs; a subject alarms when its streak reaches the bound."""

    def __init__(
        self,
        *,
        settings: ModelDelegationChainCanarySettings,
        source: ProtocolCanaryRunSource,
        streaks: ProtocolGrowthStreakStore,
        delivery: ProtocolAlarmDelivery | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._source = source
        self._streaks = streaks
        self._delivery = delivery
        self._clock = clock

    def handle(
        self, request: ModelDelegationChainCanaryCommand
    ) -> ModelDelegationChainCanaryResult:
        subject = (
            f"delegation-chain-canary:{self._settings.repository} "
            f"{self._settings.workflow}@{self._settings.branch}"
        )
        if request.fault is EnumDelegationChainCanaryFault.RED_CANARY:
            detail = (
                "the delegation chain canary on the protected delegation lane is red: "
                "run (injected) concluded (injected) at (injected)"
            )
            return self._finish(
                request,
                EnumDelegationChainCanaryOutcome.ALARM,
                subject,
                (),
                (f"{subject}: {detail}",),
                ("injected: red-canary",),
                detail,
            )
        try:
            runs = self._source.read()
        except CanaryRunsUnreadableError as exc:
            return self._finish(
                request,
                EnumDelegationChainCanaryOutcome.INDETERMINATE,
                subject,
                (),
                (),
                ("canary-runs-unreadable",),
                f"the canary runs could not be read: {exc}",
            )
        completed = [
            r
            for r in runs
            if r.status == "completed" and r.event in self._settings.events
        ]
        bound = self._settings.max_age_seconds
        breach: tuple[str, str] | None = None
        evidence = ""
        ok_reading = ""
        if not completed:
            breach = (
                "no completed run",
                f"the delegation chain canary on the protected delegation lane has not "
                f"completed a scheduled run inside {bound:g}s: no completed run of "
                f"{self._settings.workflow} on {self._settings.branch} was found",
            )
        else:
            newest = max(completed, key=lambda r: r.created_at)
            age = (self._clock() - newest.created_at).total_seconds()
            if newest.conclusion != "success":
                breach = (
                    f"run {newest.id} concluded {newest.conclusion}",
                    f"the delegation chain canary on the protected delegation lane is red: "
                    f"run {newest.id} concluded {newest.conclusion} at "
                    f"{newest.created_at.isoformat()}",
                )
            elif age > bound:
                breach = (
                    f"run {newest.id} is stale ({age:.0f}s)",
                    f"the delegation chain canary on the protected delegation lane has not "
                    f"completed a scheduled run inside {bound:g}s: newest run {newest.id} "
                    f"started {newest.created_at.isoformat()}",
                )
            else:
                ok_reading = f"run {newest.id} concluded success"
                evidence = (
                    f"newest completed run {newest.id} concluded success at "
                    f"{newest.created_at.isoformat()}, inside {bound:g}s"
                )
        stored = self._streaks.load()
        if breach is None:
            if request.write:
                stored.pop(subject, None)
                self._streaks.save(stored)
            return self._finish(
                request,
                EnumDelegationChainCanaryOutcome.OK,
                subject,
                (ok_reading,),
                (),
                (),
                evidence,
            )
        reading, detail = breach
        streak = stored.get(subject, 0) + 1
        alarms: list[str] = []
        reasons: list[str] = []
        self._classify(subject, streak, detail, alarms, reasons)
        if request.write:
            stored[subject] = streak
            self._streaks.save(stored)
        outcome = (
            EnumDelegationChainCanaryOutcome.ALARM
            if alarms
            else EnumDelegationChainCanaryOutcome.OK
        )
        return self._finish(
            request,
            outcome,
            subject,
            (reading,),
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
        request: ModelDelegationChainCanaryCommand,
        outcome: EnumDelegationChainCanaryOutcome,
        subject: str,
        readings: tuple[str, ...],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelDelegationChainCanaryResult:
        failure = ""
        if outcome is EnumDelegationChainCanaryOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumDelegationChainCanaryOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelDelegationChainCanaryResult(
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
        request: ModelDelegationChainCanaryCommand,
        result: ModelDelegationChainCanaryResult,
    ) -> ModelDelegationChainCanaryResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumDelegationChainCanaryFault.NONE
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
        result: ModelDelegationChainCanaryResult, entries: tuple[str, ...], failure: str
    ) -> ModelDelegationChainCanaryResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelDelegationChainCanaryResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["delegation_chain_canary_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "delegation_chain_canary_run_completed"
        if result.outcome is EnumDelegationChainCanaryOutcome.OK
        else "delegation_chain_canary_run_failed"
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
    "HandlerDelegationChainCanary",
    "events_for",
    "load_topics",
]
