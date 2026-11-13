# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The UPS condition: the NUT upsd serves the UPS's variables, and the alarm names the rules that tripped.

A status that is not exactly ``OL`` (``OL LB`` and ``OL CHRG`` are not OL), a battery runtime below its
low bound, or a load above the overlay's bound is one alarm line each. upsd unreachable, or a needed
variable missing or not a number, is INDETERMINATE: never OK, and the reason is named.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_ups import (
    EnumUpsFault,
    EnumUpsOutcome,
    ModelUpsCommand,
    ModelUpsResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelUpsSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolAlarmDelivery,
    ProtocolUpsReader,
    UpsUnreachableError,
    UpsUnreadableError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "ups"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerUps:
    """Read the UPS's variables from upsd and name the rules that tripped."""

    def __init__(
        self,
        *,
        settings: ModelUpsSettings,
        reader: ProtocolUpsReader,
        delivery: ProtocolAlarmDelivery | None = None,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._delivery = delivery

    def handle(self, request: ModelUpsCommand) -> ModelUpsResult:
        settings = self._settings
        ups_name = settings.ups_name
        if request.fault is not EnumUpsFault.NONE:
            return self._drill(request, ups_name)
        try:
            readings = self._reader.read(
                settings.host, settings.port, ups_name, settings.timeout_seconds
            )
        except UpsUnreachableError as exc:
            return self._finish(
                request,
                EnumUpsOutcome.INDETERMINATE,
                (),
                (f"upsd-unreachable:{type(exc).__name__}",),
                f"upsd {settings.host}:{settings.port} did not answer ({type(exc).__name__})",
                (),
            )
        except UpsUnreadableError as exc:
            return self._finish(
                request,
                EnumUpsOutcome.INDETERMINATE,
                (),
                (f"upsd-unreadable:{type(exc).__name__}",),
                f"upsd {settings.host}:{settings.port} answered unreadably ({type(exc).__name__})",
                (),
            )
        lines = tuple(f"{name}={value}" for name, value in readings.items())
        status = readings.get(settings.status_variable)
        runtime = readings.get(settings.runtime_variable)
        runtime_low = readings.get(settings.runtime_low_variable)
        load = readings.get(settings.load_variable)
        missing = tuple(
            name
            for name, value in (
                (settings.status_variable, status),
                (settings.runtime_variable, runtime),
                (settings.runtime_low_variable, runtime_low),
                (settings.load_variable, load),
            )
            if value is None
        )
        if missing:
            return self._finish(
                request,
                EnumUpsOutcome.INDETERMINATE,
                (),
                (f"missing:{name}" for name in missing),
                f"upsd answered but {ups_name} is missing {', '.join(missing)}",
                lines,
            )
        try:
            runtime_seconds = int(runtime)
            runtime_low_seconds = int(runtime_low)
            load_percent = float(load)
        except ValueError:
            return self._finish(
                request,
                EnumUpsOutcome.INDETERMINATE,
                (),
                ("not-a-number:",),
                f"{ups_name} served a variable that is not a number",
                lines,
            )
        alarms: list[str] = []
        reasons: list[str] = []
        if status != settings.ok_status:
            alarms.append(
                f"ups-status:{ups_name}: status is {status!r}, expected exactly {settings.ok_status!r}"
            )
            reasons.append("status-not-ok")
        if runtime_seconds < runtime_low_seconds:
            alarms.append(
                f"ups-runtime:{ups_name}: battery.runtime {runtime_seconds}s is below "
                f"battery.runtime.low {runtime_low_seconds}s"
            )
            reasons.append("runtime-below-low")
        if load_percent > settings.load_alarm_above_percent:
            alarms.append(
                f"ups-load:{ups_name}: ups.load {load_percent:g}% is above "
                f"{settings.load_alarm_above_percent:g}%"
            )
            reasons.append("overloaded")
        if alarms:
            return self._finish(
                request,
                EnumUpsOutcome.ALARM,
                tuple(alarms),
                tuple(reasons),
                f"{ups_name} tripped {len(alarms)} rule{'s' if len(alarms) != 1 else ''}",
                lines,
            )
        return self._finish(
            request,
            EnumUpsOutcome.OK,
            (),
            (),
            f"{ups_name} is on line power, runtime {runtime_seconds}s, load {load_percent:g}%",
            lines,
        )

    def _drill(self, request: ModelUpsCommand, ups_name: str) -> ModelUpsResult:
        settings = self._settings
        if request.fault is EnumUpsFault.ON_BATTERY:
            return self._finish(
                request,
                EnumUpsOutcome.ALARM,
                (f"ups-status:{ups_name}: status is 'OL LB', expected exactly {settings.ok_status!r}",),
                ("injected: on-battery",),
                f"drill: status reads as {settings.ok_status} LB",
                (f"{settings.status_variable}={settings.ok_status} LB",),
            )
        if request.fault is EnumUpsFault.RUNTIME_BELOW_LOW:
            return self._finish(
                request,
                EnumUpsOutcome.ALARM,
                (
                    f"ups-runtime:{ups_name}: battery.runtime 1s is below "
                    f"battery.runtime.low 2s"
                ),
                ("injected: runtime-below-low",),
                "drill: battery.runtime reads below battery.runtime.low",
                (
                    f"{settings.runtime_variable}=1",
                    f"{settings.runtime_low_variable}=2",
                ),
            )
        return self._finish(
            request,
            EnumUpsOutcome.ALARM,
            (
                f"ups-load:{ups_name}: ups.load {settings.load_alarm_above_percent + 1:g}% is above "
                f"{settings.load_alarm_above_percent:g}%"
            ),
            ("injected: overloaded",),
            "drill: ups.load reads above the overlay's bound",
            (f"{settings.load_variable}={settings.load_alarm_above_percent + 1:g}",),
        )

    def _finish(
        self,
        request: ModelUpsCommand,
        outcome: EnumUpsOutcome,
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
        readings: tuple[str, ...],
    ) -> ModelUpsResult:
        failure = ""
        if outcome is EnumUpsOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumUpsOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelUpsResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            ups_name=self._settings.ups_name,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
            readings=readings,
        )
        return self._deliver(request, result)

    def _deliver(
        self, request: ModelUpsCommand, result: ModelUpsResult
    ) -> ModelUpsResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver."""
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumUpsFault.NONE
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
        result: ModelUpsResult, entries: tuple[str, ...], failure: str
    ) -> ModelUpsResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}" if result.failure else failure,
            }
        )


def events_for(
    result: ModelUpsResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["ups_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "ups_run_completed" if result.outcome is EnumUpsOutcome.OK else "ups_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "ups_name": result.ups_name,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerUps",
    "events_for",
    "load_topics",
]
