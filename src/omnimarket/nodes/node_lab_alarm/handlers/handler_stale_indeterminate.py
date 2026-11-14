# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale-indeterminate condition: a watched condition that has read INDETERMINATE on its last runs is blind.

The old lab alarm's stale-indeterminate check, with its age counted in runs: a watched condition whose last
``after_runs`` runs all read INDETERMINATE, with no OK and no ALARM between them, alarms. Fewer runs on record
than the bound, or any verdict among them, is not stale. A run record that cannot be read makes the run
INDETERMINATE, never ALARM and never OK. The verb never watches its own condition.
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
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_stale_indeterminate import (
    EnumStaleIndeterminateFault,
    EnumStaleIndeterminateOutcome,
    ModelStaleIndeterminateCommand,
    ModelStaleIndeterminateResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelStaleIndeterminateSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolAlarmDelivery,
    ProtocolRunRecordReader,
    ProtocolRunRecordStore,
    RunRecordUnreadableError,
    RunRecordUnwritableError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "stale_indeterminate"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerStaleIndeterminate:
    """Read the run records of the watched conditions; one alarms when its last runs are all INDETERMINATE."""

    def __init__(
        self,
        *,
        settings: ModelStaleIndeterminateSettings,
        reader: ProtocolRunRecordReader,
        delivery: ProtocolAlarmDelivery | None = None,
        records: ProtocolRunRecordStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._delivery = delivery
        self._records = records
        self._clock = clock

    def handle(
        self, request: ModelStaleIndeterminateCommand
    ) -> ModelStaleIndeterminateResult:
        conditions = tuple(w.condition for w in self._settings.watched)
        if request.fault is EnumStaleIndeterminateFault.PLANTED_STALE:
            first = conditions[0]
            detail = (
                f"condition {first} has read INDETERMINATE on its last "
                f"{self._settings.after_runs} runs, with no OK and no ALARM to clear it "
                f"-- an alarm that cannot tell is not the same as one that found nothing wrong "
                f"(injected: planted-stale)"
            )
            return self._finish(
                request,
                EnumStaleIndeterminateOutcome.ALARM,
                conditions,
                (),
                (f"stale-indeterminate:{first}: {detail}",),
                (),
                detail,
            )
        try:
            outcomes = {
                w.condition: self._reader.recent_outcomes(
                    w.condition, self._settings.after_runs
                )
                for w in self._settings.watched
            }
        except RunRecordUnreadableError as exc:
            return self._finish(
                request,
                EnumStaleIndeterminateOutcome.INDETERMINATE,
                conditions,
                (),
                (),
                (f"record-unreadable:{exc}",),
                f"the run record store could not be read: {exc}",
            )
        alarms: list[str] = []
        reasons: list[str] = []
        readings: list[str] = []
        for w in self._settings.watched:
            cond = w.condition
            results = outcomes[cond]
            if len(results) < self._settings.after_runs:
                readings.append(
                    f"{cond}: {len(results)} of {self._settings.after_runs} runs on record"
                )
                continue
            if all(r == "INDETERMINATE" for r in results):
                detail = (
                    f"condition {cond} has read INDETERMINATE on its last "
                    f"{self._settings.after_runs} runs, with no OK and no ALARM to clear it "
                    f"-- an alarm that cannot tell is not the same as one that found nothing wrong"
                )
                alarms.append(f"stale-indeterminate:{cond}: {detail}")
                readings.append(f"{cond}: stale")
            else:
                readings.append(f"{cond}: reads a verdict")
        outcome = (
            EnumStaleIndeterminateOutcome.ALARM
            if alarms
            else EnumStaleIndeterminateOutcome.OK
        )
        evidence = "; ".join(readings) if readings else "no conditions declared"
        return self._finish(
            request,
            outcome,
            conditions,
            tuple(readings),
            tuple(alarms),
            tuple(reasons),
            evidence,
        )

    def _finish(
        self,
        request: ModelStaleIndeterminateCommand,
        outcome: EnumStaleIndeterminateOutcome,
        conditions: tuple[str, ...],
        readings: tuple[str, ...],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelStaleIndeterminateResult:
        failure = ""
        if outcome is EnumStaleIndeterminateOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumStaleIndeterminateOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelStaleIndeterminateResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            conditions=conditions,
            readings=readings,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._record(request, self._deliver(request, result))

    def _record(
        self,
        request: ModelStaleIndeterminateCommand,
        result: ModelStaleIndeterminateResult,
    ) -> ModelStaleIndeterminateResult:
        """Append the finished run to the run record the stale-indeterminate verb reads; a probe and a drill never write to it.

        A record that cannot be written is a typed failure of the run, never a silent skip: the stale-indeterminate
        verb would otherwise go on reading old lines as if they were this run's.
        """
        if (
            self._records is None
            or not request.write
            or request.fault is not EnumStaleIndeterminateFault.NONE
        ):
            return result
        said = (
            result.evidence
            if result.outcome.value == "OK"
            else "; ".join(result.alarms or result.reasons) or result.evidence
        )
        try:
            self._records.append(
                {
                    "finished_at": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reports": [
                        {
                            "condition": CONDITION,
                            "outcome": result.outcome.value,
                            "evidence": said,
                        }
                    ],
                }
            )
        except RunRecordUnwritableError as exc:
            return result.model_copy(update={"failure": f"record-unwritable:{exc}"})
        return result

    def _deliver(
        self,
        request: ModelStaleIndeterminateCommand,
        result: ModelStaleIndeterminateResult,
    ) -> ModelStaleIndeterminateResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumStaleIndeterminateFault.NONE
        ):
            return result
        try:
            typed = []
            for line in result.alarms:
                subject, separator, detail = line.partition(": ")
                if not separator:
                    raise ValueError(
                        f"alarm line {line!r} names no subject before ': '"
                    )
                typed.append(
                    ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
                )
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
        result: ModelStaleIndeterminateResult, entries: tuple[str, ...], failure: str
    ) -> ModelStaleIndeterminateResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelStaleIndeterminateResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["stale_indeterminate_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "stale_indeterminate_run_completed"
        if result.outcome is EnumStaleIndeterminateOutcome.OK
        else "stale_indeterminate_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "conditions": list(result.conditions),
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerStaleIndeterminate",
    "events_for",
    "load_topics",
]
