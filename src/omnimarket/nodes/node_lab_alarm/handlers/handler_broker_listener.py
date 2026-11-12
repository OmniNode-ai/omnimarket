# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker-listener condition: the dev broker's external listener answers a Kafka ApiVersions request.

The old lab alarm's third condition, ported with its rule unchanged: one answering bootstrap address is
enough, as it is for a client. A name the alarm host cannot resolve is INDETERMINATE: that is the alarm
host's fault, not evidence about the broker. Refused, timed out or closed on every address is an ALARM.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_broker_listener import (
    EnumBrokerListenerFault,
    EnumBrokerListenerOutcome,
    ModelBrokerListenerCommand,
    ModelBrokerListenerResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelBrokerListenerSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    ListenerRefusedError,
    ListenerUnresolvableError,
    ProtocolAlarmDelivery,
    ProtocolListenerProbe,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.models.model_lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "broker_listener"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerBrokerListener:
    """Probe every bootstrap address in order; the first that answers is OK, every refusal is an ALARM."""

    def __init__(
        self,
        *,
        settings: ModelBrokerListenerSettings,
        probe: ProtocolListenerProbe,
        delivery: ProtocolAlarmDelivery | None = None,
    ) -> None:
        self._settings = settings
        self._probe = probe
        self._delivery = delivery

    def handle(self, request: ModelBrokerListenerCommand) -> ModelBrokerListenerResult:
        bootstrap = self._settings.bootstrap_servers
        addresses: list[tuple[str, int]] = []
        for entry in (part.strip() for part in bootstrap.split(",")):
            name, _, port_text = entry.rpartition(":")
            if not name or not port_text.isdigit():
                return self._finish(
                    request,
                    EnumBrokerListenerOutcome.INDETERMINATE,
                    (),
                    ("bootstrap-malformed",),
                    f"bootstrap {bootstrap!r} is not host:port[,host:port]",
                )
            addresses.append((name, int(port_text)))
        if request.fault is EnumBrokerListenerFault.LISTENER_REFUSED:
            injected = [
                f"{host}:{port} (injected: listener-refused)"
                for host, port in addresses
            ]
            detail = self._alarm_detail(bootstrap, injected)
            return self._finish(
                request,
                EnumBrokerListenerOutcome.ALARM,
                (f"broker-listener:{bootstrap}: {detail}",),
                ("injected: listener-refused",),
                detail,
            )
        refused: list[str] = []
        unresolved: list[str] = []
        for host, port in addresses:
            try:
                self._probe.probe(host, port, self._settings.timeout_seconds)
            except ListenerUnresolvableError as exc:
                unresolved.append(f"{host}:{port} ({exc})")
                continue
            except ListenerRefusedError as exc:
                refused.append(f"{host}:{port} ({exc})")
                continue
            return self._finish(
                request,
                EnumBrokerListenerOutcome.OK,
                (),
                (),
                f"external listener {host}:{port} answered a Kafka ApiVersions "
                f"request (bootstrap {bootstrap}); unauthenticated, not a "
                "topic read",
            )
        if unresolved:
            return self._finish(
                request,
                EnumBrokerListenerOutcome.INDETERMINATE,
                (),
                tuple(f"unresolved:{line.partition(' (')[0]}" for line in unresolved),
                f"could not resolve {'; '.join(unresolved)}"
                + (f"; refused: {'; '.join(refused)}" if refused else ""),
            )
        detail = self._alarm_detail(bootstrap, refused)
        return self._finish(
            request,
            EnumBrokerListenerOutcome.ALARM,
            (f"broker-listener:{bootstrap}: {detail}",),
            (),
            detail,
        )

    def _alarm_detail(self, bootstrap: str, refused: list[str]) -> str:
        return (
            f"the dev broker's external listener {bootstrap} did not answer "
            f"a Kafka ApiVersions request within {self._settings.timeout_seconds:g}s: "
            f"{'; '.join(refused)}"
        )

    def _finish(
        self,
        request: ModelBrokerListenerCommand,
        outcome: EnumBrokerListenerOutcome,
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelBrokerListenerResult:
        failure = ""
        if outcome is EnumBrokerListenerOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumBrokerListenerOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelBrokerListenerResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            bootstrap_servers=self._settings.bootstrap_servers,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._deliver(request, result)

    def _deliver(
        self, request: ModelBrokerListenerCommand, result: ModelBrokerListenerResult
    ) -> ModelBrokerListenerResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumBrokerListenerFault.NONE
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
        result: ModelBrokerListenerResult, entries: tuple[str, ...], failure: str
    ) -> ModelBrokerListenerResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelBrokerListenerResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["broker_listener_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "broker_listener_run_completed"
        if result.outcome is EnumBrokerListenerOutcome.OK
        else "broker_listener_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "bootstrap_servers": result.bootstrap_servers,
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerBrokerListener",
    "events_for",
    "load_topics",
]
