# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one delegation check (C13, C14 or C17) once and put the run on the bus (OMN-26043).

``handle(request) -> result`` sends the check's task through the delegation responder chain the settings store declares
(local least busy, then OpenRouter, then the Claude API; no check names one model), grades what the customer machine
observed, builds the run record (``ModelMvpRun``-shaped, so the beta board reads the check from the bus) and publishes it on
the overlay's result topic. A task that never reached a local responder reads NOT_MEASURED with the chain it tried; a
collector the machine lacks reads PRECONDITION-ABSENT; a probe that raises is an ERROR run. None is green, and every one is
still emitted: a broken probe must read red, never silent.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    resolve_chain,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    CUSTOMER_LOCAL_CHECKS,
    GATEWAY_ONLY_CHECKS,
    GatewayRunSettings,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_c13,
    grade_c14,
    grade_c17,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CAUSE_LAB_MODEL,
    CAUSE_PRODUCT,
    EnumCheckVerdict,
    ModelCheckPrecondition,
    ModelDelegationCheckReading,
    ModelDelegationCheckRequest,
    ModelDelegationCheckResult,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
    ProtocolCheckResultPublisher,
    ProtocolCustomerMachine,
    ProtocolGatewayClient,
    ProtocolResponderReader,
)

NODE = "node_delegation_check_probe_effect"
EVENT_TYPE = "delegation_check.probe.completed"


def contract_topics() -> tuple[str, ...]:
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return tuple(str(t) for t in contract["event_bus"]["publish_topics"])


def _new_run_id(check_id: str, now: datetime) -> str:
    return f"{check_id.lower()}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class HandlerDelegationCheckProbe:
    def __init__(
        self,
        *,
        responders: Sequence[ModelDelegationCheckResponder],
        reader: ProtocolResponderReader,
        machine: ProtocolCustomerMachine,
        publisher: ProtocolCheckResultPublisher,
        topic: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[str, datetime], str] = _new_run_id,
        gateway: ProtocolGatewayClient | None = None,
        gateway_settings: GatewayRunSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._responders = tuple(responders)
        self._reader = reader
        self._machine = machine
        self._publisher = publisher
        self._topic = topic
        self._clock = clock
        self._run_id = run_id_factory
        self._gateway = gateway
        self._gateway_settings = gateway_settings or GatewayRunSettings()
        self._sleep = sleep
        self._monotonic = monotonic

    def _gateway_check(
        self, request: ModelDelegationCheckRequest
    ) -> ModelDelegationCheckReading:
        """A check a cloud workflow used to grade: graded by requests to the settings store's gateway, and only to it."""
        if self._gateway is None:
            return ModelDelegationCheckReading(
                preconditions=(
                    ModelCheckPrecondition(
                        name="gateway_row",
                        present=False,
                        detail="the settings store serves no delegation_check_gateway row, so no gateway address is known",
                    ),
                )
            )
        try:
            return run_gateway_check(
                request.check_id,
                self._gateway,
                self._gateway_settings,
                sleep=self._sleep,
                monotonic=self._monotonic,
            )
        except CollectorAbsentError as exc:
            # The row was read; the run keeps the address and the tenant beside the credential that is absent.
            return ModelDelegationCheckReading(
                endpoint=self._gateway.gateway_url,
                tenant_slug=self._gateway.tenant_slug,
                preconditions=(
                    ModelCheckPrecondition(
                        name=exc.name, present=False, detail=exc.detail
                    ),
                ),
            )

    @staticmethod
    def _chain_head(resolution: ModelChainResolution) -> dict[str, object]:
        return {
            "chain": resolution.attempts,
            "chosen_tier": resolution.tier,
            "chosen_responder": resolution.base_url,
            "served_model": resolution.served_model,
            "window": resolution.window,
            "capabilities": resolution.capabilities,
            "input_usd_per_million": resolution.input_usd_per_million,
            "output_usd_per_million": resolution.output_usd_per_million,
        }

    def _task_check(
        self, request: ModelDelegationCheckRequest
    ) -> ModelDelegationCheckReading:
        resolution = resolve_chain(self._responders, self._reader.read)
        head = self._chain_head(resolution)
        if not resolution.is_local or not resolution.base_url:
            tried = ", ".join(
                f"{a.base_url or a.tier}:{a.outcome}" for a in resolution.attempts
            )
            return ModelDelegationCheckReading(
                **head,
                not_measured=f"no local responder served a model with a window; the chain advanced to {resolution.tier} ({tried})",
            )
        assert resolution.served_model is not None
        try:
            observation = self._machine.run_delegation(request.check_id, resolution)
            if request.check_id == "C13":
                clauses = grade_c13(
                    observation,
                    responder_base=resolution.base_url,
                    served_model=resolution.served_model,
                )
            else:
                clauses = grade_c14(
                    observation, self._machine.collect_routing(resolution)
                )
        except CollectorAbsentError as exc:
            # The chain was read live; the run keeps it beside the absent collector.
            return ModelDelegationCheckReading(
                **head,
                preconditions=(
                    ModelCheckPrecondition(
                        name=exc.name, present=False, detail=exc.detail
                    ),
                ),
            )
        return ModelDelegationCheckReading(**head, clauses=clauses)

    def _surface_check(self) -> ModelDelegationCheckReading:
        # The surface check grades no model, so a chain that left the local tier never turns it red; it still runs through
        # the chain and records it, so every check's record names the responder the lab served when it ran.
        head = self._chain_head(resolve_chain(self._responders, self._reader.read))
        try:
            observation = self._machine.collect_surface()
        except CollectorAbsentError as exc:
            # The chain was read live; the run keeps it beside the absent collector.
            return ModelDelegationCheckReading(
                **head,
                preconditions=(
                    ModelCheckPrecondition(
                        name=exc.name, present=False, detail=exc.detail
                    ),
                ),
            )
        # An install that did not complete installed no core to read: that is the install clause failing, which must read FAIL
        # (the product's own line did not install), never an absent instrument.
        installed = observation.install_rc == 0
        precondition = ModelCheckPrecondition(
            name="operator_switch_named",
            present=observation.operator_switch_env is not None or not installed,
            detail="the installed core names the switch that shows every command group"
            if observation.operator_switch_env
            else "the installed core was not read for its operator switch"
            if installed
            else "the guide's install line did not complete, so no core was installed to read",
        )
        return ModelDelegationCheckReading(
            **head, clauses=grade_c17(observation), preconditions=(precondition,)
        )

    def _with_gateway_leg(
        self, reading: ModelDelegationCheckReading, request: ModelDelegationCheckRequest
    ) -> ModelDelegationCheckReading:
        """A customer-local check is also read from the gateway: its clauses and absent surfaces join the machine reading's."""
        assert request.check_id in CUSTOMER_LOCAL_CHECKS
        leg = self._gateway_check(request)
        return reading.model_copy(
            update={
                "clauses": (*reading.clauses, *leg.clauses),
                "preconditions": (*reading.preconditions, *leg.preconditions),
                "not_measured": reading.not_measured or leg.not_measured,
                "endpoint": leg.endpoint,
                "tenant_slug": leg.tenant_slug,
            }
        )

    def _reading(
        self, request: ModelDelegationCheckRequest
    ) -> ModelDelegationCheckReading:
        if request.check_id in GATEWAY_ONLY_CHECKS:
            reading = self._gateway_check(request)
        else:
            try:
                reading = (
                    self._surface_check()
                    if request.check_id == "C17"
                    else self._task_check(request)
                )
            except CollectorAbsentError as exc:
                reading = ModelDelegationCheckReading(
                    preconditions=(
                        ModelCheckPrecondition(
                            name=exc.name, present=False, detail=exc.detail
                        ),
                    )
                )
            reading = self._with_gateway_leg(reading, request)
        return reading.model_copy(update={"versions": self._machine.versions()})

    def handle(
        self, request: ModelDelegationCheckRequest
    ) -> ModelDelegationCheckResult:
        created = self._clock()
        error: str | None = None
        try:
            reading = self._reading(request)
        except CollectorAbsentError as exc:
            reading = ModelDelegationCheckReading(
                preconditions=(
                    ModelCheckPrecondition(
                        name=exc.name, present=False, detail=exc.detail
                    ),
                )
            )
        except Exception as exc:  # the run is recorded as an ERROR, never lost
            reading = ModelDelegationCheckReading()
            error = f"{type(exc).__name__}: {exc}"[-2000:]
        verdict = EnumCheckVerdict.ERROR if error else reading.verdict()
        if verdict is EnumCheckVerdict.ERROR and error is None:
            error = "no clause was graded and nothing was absent"
        cause = {
            EnumCheckVerdict.FAIL: CAUSE_PRODUCT,
            EnumCheckVerdict.NOT_MEASURED: CAUSE_LAB_MODEL,
        }.get(verdict)
        result = ModelDelegationCheckResult(
            correlation_id=request.correlation_id,
            check_id=request.check_id,
            run_id=self._run_id(request.check_id, created),
            created_at=created,
            finished_at=self._clock(),
            event=request.trigger,
            status="errored" if verdict is EnumCheckVerdict.ERROR else "completed",
            conclusion="success" if verdict is EnumCheckVerdict.PASS else "failure",
            verdict=verdict,
            cause_class=cause,
            clauses=reading.clauses,
            preconditions=reading.preconditions,
            chain=reading.chain,
            chosen_tier=reading.chosen_tier,
            chosen_responder=reading.chosen_responder,
            served_model=reading.served_model,
            window=reading.window,
            capabilities=reading.capabilities,
            input_usd_per_million=reading.input_usd_per_million,
            output_usd_per_million=reading.output_usd_per_million,
            versions=reading.versions,
            gateway_url=reading.endpoint,
            tenant_slug=reading.tenant_slug,
            error=error
            or (
                reading.not_measured
                if verdict is EnumCheckVerdict.NOT_MEASURED
                else None
            ),
        )
        accepted = self._publisher.publish(
            topic=self._topic,
            event_type=EVENT_TYPE,
            payload={
                **result.model_dump(mode="json", exclude={"emitted"}),
                "criterion": request.check_id,
                "mvp_run": result.mvp_run(),
            },
            event_id=result.run_id,
            correlation_id=request.correlation_id,
            partition_key=request.check_id,
        )
        return result.model_copy(update={"emitted": accepted})


__all__ = [
    "EVENT_TYPE",
    "NODE",
    "HandlerDelegationCheckProbe",
    "ModelChainResolution",
    "contract_topics",
]
