# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-census conditions: lane drift and container health from the deploy agent's census.

The old lab alarm's lane census, ported with its rule unchanged: a census event that is unreadable,
invalid, not a census event, has a mismatched alert key, or is stale is INDETERMINATE, never ALARM
and never OK. Findings of the condition alarm with one line naming the alert key; no findings is OK.
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
from omnimarket.nodes.node_lab_alarm.handlers.lane_census_snapshot import (
    FINDING_KIND_SEVERITY,
    alert_key,
    validate_event,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
    EnumLaneCensusFault,
    EnumLaneCensusOutcome,
    ModelLaneCensusCommand,
    ModelLaneCensusResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLaneCensusSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    CensusUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolCensusSource,
    ProtocolRunRecordStore,
    RunRecordUnwritableError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerLaneCensus:
    """Read the deploy agent's census; findings of the condition alarm with one line."""

    def __init__(
        self,
        *,
        condition: EnumLaneCensusCondition,
        settings: ModelLaneCensusSettings,
        source: ProtocolCensusSource,
        delivery: ProtocolAlarmDelivery | None = None,
        records: ProtocolRunRecordStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._condition = condition
        self._settings = settings
        self._source = source
        self._delivery = delivery
        self._records = records
        self._clock = clock

    def handle(self, request: ModelLaneCensusCommand) -> ModelLaneCensusResult:
        condition = self._condition
        if request.fault is EnumLaneCensusFault.PLANTED_FINDING:
            detail = (
                f"planted finding: lane (injected)/container (injected): "
                f"{condition.value} -- (injected)"
            )
            return self._finish(
                request,
                EnumLaneCensusOutcome.ALARM,
                "planted",
                (f"planted: {detail}",),
                (f"planted: {detail}",),
                (),
                f"planted: {detail}",
            )
        try:
            snapshot = self._source.read()
        except CensusUnreadableError as exc:
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                "",
                (),
                (),
                ("census-unreadable",),
                f"lane census unreadable: {exc}",
            )
        errors = validate_event(snapshot, kind_severity=FINDING_KIND_SEVERITY)
        if errors:
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                "",
                (),
                (),
                ("census-invalid",),
                "; ".join(errors),
            )
        if snapshot.get("event_type") != "lane-census-drift" or not snapshot.get(
            "host"
        ):
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                "",
                (),
                (),
                ("census-not-a-census-event",),
                "the snapshot is not a lane-census event",
            )
        host = snapshot["host"]
        key = alert_key(host, snapshot)
        if snapshot.get("alert_key") != key:
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                host,
                (),
                (),
                ("census-alert-key-mismatch",),
                f"the alert key {snapshot.get('alert_key')!r} does not match {key!r}",
            )
        emitted_at = snapshot.get("emitted_at")
        if not isinstance(emitted_at, str):
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                host,
                (),
                (),
                ("census-invalid",),
                "the snapshot has no parseable emitted_at",
            )
        try:
            emitted = datetime.fromisoformat(emitted_at)
        except ValueError:
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                host,
                (),
                (),
                ("census-invalid",),
                f"the snapshot's emitted_at {emitted_at!r} is not parseable",
            )
        if emitted.tzinfo is None:
            emitted = emitted.replace(tzinfo=UTC)
        age = self._clock() - emitted
        if age < timedelta(seconds=-1) or age > timedelta(
            seconds=self._settings.max_age_seconds
        ):
            return self._finish(
                request,
                EnumLaneCensusOutcome.INDETERMINATE,
                host,
                (),
                (),
                ("census-stale",),
                f"the census is {age.total_seconds():.0f}s old, past the "
                f"{self._settings.max_age_seconds:g}s bound",
            )
        findings = [
            f
            for f in snapshot.get("findings", [])
            if (
                f.get("kind") == "container_unhealthy"
                if condition is EnumLaneCensusCondition.CONTAINER_UNHEALTHY
                else f.get("kind") != "container_unhealthy"
            )
        ]
        if not findings:
            evidence = (
                f"census host={host} emitted_at={emitted_at} alert_key={key}; "
                f"0 {condition.value} findings"
            )
            return self._finish(
                request,
                EnumLaneCensusOutcome.OK,
                host,
                (),
                (),
                (),
                evidence,
            )
        readings = tuple(
            f"{f['lane']}/{f['container']}: {f['kind']} -- {f['detail']}"
            for f in findings
        )
        alarm = f"{key}: " + "; ".join(readings)
        return self._finish(
            request,
            EnumLaneCensusOutcome.ALARM,
            host,
            readings,
            (alarm,),
            (),
            alarm,
        )

    def _finish(
        self,
        request: ModelLaneCensusCommand,
        outcome: EnumLaneCensusOutcome,
        host: str,
        readings: tuple[str, ...],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelLaneCensusResult:
        condition = self._condition
        failure = ""
        if outcome is EnumLaneCensusOutcome.ALARM:
            failure = f"alarm:{condition.value}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumLaneCensusOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelLaneCensusResult(
            correlation_id=request.correlation_id,
            condition=condition,
            outcome=outcome,
            fault=request.fault,
            host=host,
            readings=readings,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._record(request, self._deliver(request, result))

    def _record(
        self, request: ModelLaneCensusCommand, result: ModelLaneCensusResult
    ) -> ModelLaneCensusResult:
        """Append the finished run to the run record the stale-indeterminate verb reads; a probe and a drill never write to it.

        A record that cannot be written is a typed failure of the run, never a silent skip: the stale-indeterminate
        verb would otherwise go on reading old lines as if they were this run's.
        """
        if (
            self._records is None
            or not request.write
            or request.fault is not EnumLaneCensusFault.NONE
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
                            "condition": self._condition.value,
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
        self, request: ModelLaneCensusCommand, result: ModelLaneCensusResult
    ) -> ModelLaneCensusResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumLaneCensusFault.NONE
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
                    ModelLabAlarm(
                        condition=result.condition.value,
                        subject=subject,
                        detail=detail,
                    )
                )
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=result.condition.value,
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
        result: ModelLaneCensusResult, entries: tuple[str, ...], failure: str
    ) -> ModelLaneCensusResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelLaneCensusResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    prefix = result.condition.value
    chain: list[tuple[str, dict[str, object]]] = [
        (topics[f"{prefix}_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        f"{prefix}_run_completed"
        if result.outcome is EnumLaneCensusOutcome.OK
        else f"{prefix}_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "host": result.host,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "HandlerLaneCensus",
    "events_for",
    "load_topics",
]
