# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The container-restarts condition: a lane container restarted more often than its declared bound.

The old lab alarm's second condition, ported with its rule unchanged: each service of the census lane that declares a
``restart_bound`` in the lane manifest is read once, in name order, and a restart count strictly above its own bound is an
ALARM. A container that cannot be read (absent, docker unreachable, an answer that is not ``<count> <status>``) is
INDETERMINATE and dominates: a partial read that found nothing wrong says nothing about the containers it could not reach.
A lane with no declared bound is INDETERMINATE too, never healthy.
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
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_container_restarts import (
    EnumContainerRestartsFault,
    EnumContainerRestartsOutcome,
    ModelContainerRestartsCommand,
    ModelContainerRestartsResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelContainerRestartsSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ContainerUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolContainerRestartReader,
    ProtocolRestartBoundSource,
    RestartBoundsUnreadableError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "container_restarts"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerContainerRestarts:
    """Read every declared container's restart count against its own bound; unreadable dominates an alarm."""

    def __init__(
        self,
        *,
        settings: ModelContainerRestartsSettings,
        bounds: ProtocolRestartBoundSource,
        reader: ProtocolContainerRestartReader,
        delivery: ProtocolAlarmDelivery | None = None,
    ) -> None:
        self._settings = settings
        self._bounds = bounds
        self._reader = reader
        self._delivery = delivery

    def handle(
        self, request: ModelContainerRestartsCommand
    ) -> ModelContainerRestartsResult:
        try:
            bounds = self._bounds.bounds()
        except RestartBoundsUnreadableError as exc:
            return self._finish(
                request,
                EnumContainerRestartsOutcome.INDETERMINATE,
                (),
                (),
                ("bounds-unreadable",),
                f"the restart bounds could not be read: {exc}",
            )
        if not bounds:
            return self._finish(
                request,
                EnumContainerRestartsOutcome.INDETERMINATE,
                (),
                (),
                ("no-bounds",),
                "no container carries a declared restart bound, so nothing was "
                "checked; an empty subject list is not a healthy lane",
            )
        if request.fault is EnumContainerRestartsFault.RESTARTS_OVER_BOUND:
            container = sorted(bounds)[0]
            restarts = bounds[container] + 1
            line = self._alarm_line(container, restarts, bounds[container], "running")
            return self._finish(
                request,
                EnumContainerRestartsOutcome.ALARM,
                (line,),
                (f"{container}={restarts}/{bounds[container]} running",),
                ("injected: restarts-over-bound",),
                f"restart counts read: {container}={restarts}/{bounds[container]} running (injected)",
            )
        alarms: list[str] = []
        unreadable: list[str] = []
        readings: list[str] = []
        for container in sorted(bounds):
            bound = bounds[container]
            try:
                restarts, status = self._reader.read(
                    container, self._settings.timeout_seconds
                )
            except ContainerUnreadableError as exc:
                unreadable.append(f"{container} ({exc})")
                continue
            readings.append(f"{container}={restarts}/{bound} {status}")
            if restarts > bound:
                alarms.append(self._alarm_line(container, restarts, bound, status))
        evidence = f"restart counts read: {', '.join(readings) or 'none'}"
        if unreadable:
            return self._finish(
                request,
                EnumContainerRestartsOutcome.INDETERMINATE,
                (),
                tuple(readings),
                tuple(f"unreadable:{line.partition(' (')[0]}" for line in unreadable),
                f"{evidence}; UNREADABLE: {'; '.join(unreadable)}",
            )
        if alarms:
            return self._finish(
                request,
                EnumContainerRestartsOutcome.ALARM,
                tuple(alarms),
                tuple(readings),
                (),
                evidence,
            )
        return self._finish(
            request,
            EnumContainerRestartsOutcome.OK,
            (),
            tuple(readings),
            (),
            f"{evidence}; every container is inside its declared bound",
        )

    @staticmethod
    def _alarm_line(container: str, restarts: int, bound: int, status: str) -> str:
        return (
            f"{container}: container {container} has restarted {restarts} times, "
            f"past its declared bound of {bound}; status {status}"
        )

    def _finish(
        self,
        request: ModelContainerRestartsCommand,
        outcome: EnumContainerRestartsOutcome,
        alarms: tuple[str, ...],
        readings: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelContainerRestartsResult:
        failure = ""
        if outcome is EnumContainerRestartsOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumContainerRestartsOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelContainerRestartsResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            census_lane=self._settings.census_lane,
            alarms=alarms,
            readings=readings,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._deliver(request, result)

    def _deliver(
        self,
        request: ModelContainerRestartsCommand,
        result: ModelContainerRestartsResult,
    ) -> ModelContainerRestartsResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumContainerRestartsFault.NONE
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
        result: ModelContainerRestartsResult, entries: tuple[str, ...], failure: str
    ) -> ModelContainerRestartsResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelContainerRestartsResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["container_restarts_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "container_restarts_run_completed"
        if result.outcome is EnumContainerRestartsOutcome.OK
        else "container_restarts_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "census_lane": result.census_lane,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerContainerRestarts",
    "events_for",
    "load_topics",
]
