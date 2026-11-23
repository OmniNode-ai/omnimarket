# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R9, malformed and injected tasks are refused (OMN-24554, readiness plan 2.2).

One run sends each committed bad request of the C11 corpus (cases E1 to E6 and E11) as a raw bus command to the
delegation runtime's command topic and reads the typed refusal terminal the runtime answers with. It holds only when the
terminal failed, its failure_code is the declared refusal code, its retryable flag is the declared one and its
failure_reason names the declared field where one is named (E4's reason is redacted by the runtime, so it names none).
The positive control runs first: the probe tenant's totals are read over R8's bus lane — R9 declares no overlay document
of its own and reads R8's tenant_read block, the same bus, lane and probe tenant — so a bus the corpus payloads cannot
reach can never read as all refused. Every assertion compares typed fields; no model judges a pass. The result goes to
the ledger first (one STATUS row, the row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import os
import shlex
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.handlers.delegation_readiness_probe import ScriptedLegTransport
from omnimarket.handlers.delegation_readiness_scenario import (
    ModelReadinessRuntime,
    ReadinessScenarioBase,
    load_terminal_topic,
    read_contract,
)
from omnimarket.handlers.delegation_readiness_scenario import (
    load_schedule as load_schedule_from,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
)
from omnimarket.handlers.delegation_scenario_settings import load_tenant_read_overlay
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    LaneReadError,
    ModelTotals,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    LANE_BUILDERS,
    load_lane_settings,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.adapter_r9_corpus import (
    CorpusRequest,
    ModelCorpusAnswer,
    build_corpus_request,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r9_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"

#: R9 declares no overlay document of its own: its control lane and its corpus request read R8's tenant_read block,
#: the same bus, lane and probe tenant R8's own tenant reads use.
R8_NODE_NAME = "node_delegation_scenario_r8_effect"
NO_R8_TENANT_READ = (
    f"the R8 overlay document ({R8_NODE_NAME}) names no tenant_read block"
)

Clock = Callable[[], datetime]
Check = Callable[[str, bool, str], None]


class ModelScenarioR9RunCommand(BaseModel):
    """One run of scenario R9. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelR9Case(BaseModel):
    """One committed bad request: its id, its defect, the payload exactly as it is published, and the field its refusal names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    defect: str = Field(min_length=1)
    payload: dict[str, JsonValue]
    #: The field the refusal's failure_reason must name; ``None`` where the runtime redacts the reason (E4).
    names_field: str | None = Field(default=None, min_length=1)


class ModelR9Inputs(BaseModel):
    """The committed inputs file of scenario R9."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_failure_code: str = Field(min_length=1)
    retryable: bool
    #: How long each corpus payload waits for its refusal terminal.
    wait_timeout_seconds: float = Field(gt=0)
    cases: tuple[ModelR9Case, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _case_ids_unique(self) -> Self:
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError(f"case ids are not unique: {ids}")
        return self


class ModelR9BusSettings(BaseModel):
    """The contract's ``bus`` block: the node whose contract names the command topic the corpus is published on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_node: str = Field(min_length=1)
    workspace_env: str = Field(min_length=1)


def load_inputs(path: Path) -> ModelR9Inputs:
    return ModelR9Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessRuntime:
    return ModelReadinessRuntime.model_validate(read_contract(path)["runtime"])


def load_bus_settings(path: Path = CONTRACT_PATH) -> ModelR9BusSettings:
    return ModelR9BusSettings.model_validate(read_contract(path)["bus"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


def _no_overlay_request(
    payload: Mapping[str, JsonValue], timeout_s: float
) -> ModelCorpusAnswer:
    """The request when the R8 overlay document names no tenant_read block: nothing was published, nothing answered.

    The control refuses first and the scenario returns before any case runs, so this answer is the typed floor that
    keeps a missing overlay document a failed run instead of a crash.
    """
    del payload, timeout_s
    return ModelCorpusAnswer(
        answered=False, failed=False, payload={}, reason=NO_R8_TENANT_READ
    )


class HandlerScenarioR9(ReadinessScenarioBase):
    """Run scenario R9 once, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelReadinessRuntime,
        *,
        home: Path,
        environment: Mapping[str, str],
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str,
        request: CorpusRequest,
        control: Callable[[], ModelTotals],
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
        inputs_path: Path | None = None,
        check_dev_ready: bool = False,
    ) -> None:
        super().__init__(
            runtime,
            home=home,
            environment=environment,
            appender=appender,
            sink=sink,
            terminal_topic=terminal_topic,
            # R9 runs no probe ladder: the base's leg transport is never built.
            transport_factory=lambda: ScriptedLegTransport(),
            trigger=trigger,
            clock=clock,
            check_dev_ready=check_dev_ready,
        )
        self._inputs_path = inputs_path or NODE_DIR / runtime.inputs_relpath
        self._request = request
        self._control = control

    def handle(self, request: ModelScenarioR9RunCommand) -> ModelScenarioResult:
        return self.run()

    # -- the scenario ---------------------------------------------------------------------------------------------

    def _scenario(self, assertions: list[str]) -> str:
        problems: list[str] = []

        def check(name: str, held: bool, why: str = "") -> None:
            assertions.append(("+" if held else "-") + name)
            if not held and why:
                problems.append(f"{name}: {why}")

        try:
            inputs = load_inputs(self._inputs_path)
        except (OSError, ValueError) as exc:
            assertions.append("-inputs_readable")
            return f"inputs unreadable: {type(exc).__name__}"
        assertions.append("+inputs_readable")

        # The control runs first: a bus the corpus payloads cannot reach must never read as all refused.
        try:
            self._control()
        except LaneReadError as exc:
            assertions.append("-control_read_completed")
            return f"the control lane did not read: {exc}"
        assertions.append("+control_read_completed")

        for case in inputs.cases:
            answer = self._request(case.payload, inputs.wait_timeout_seconds)
            self._check_case(case, inputs, answer, check)
        return "; ".join(problems) or "every assertion held"

    @staticmethod
    def _check_case(
        case: ModelR9Case,
        inputs: ModelR9Inputs,
        answer: ModelCorpusAnswer,
        check: Check,
    ) -> None:
        code = answer.payload.get("failure_code")
        check(
            f"case_{case.id}_refused",
            answer.answered and answer.failed and code == inputs.refusal_failure_code,
            HandlerScenarioR9._refusal_detail(answer, code, inputs),
        )
        retryable = answer.payload.get("retryable")
        check(
            f"case_{case.id}_not_retryable",
            retryable == inputs.retryable,
            f"retryable {retryable!r}, want {inputs.retryable!r}",
        )
        if case.names_field is None:
            return
        reason = answer.payload.get("failure_reason")
        check(
            f"case_{case.id}_names_field",
            isinstance(reason, str) and case.names_field in reason,
            f"failure_reason {reason!r} names no {case.names_field}",
        )

    @staticmethod
    def _refusal_detail(
        answer: ModelCorpusAnswer, code: JsonValue | None, inputs: ModelR9Inputs
    ) -> str:
        if not answer.answered:
            return f"the case was not answered: {answer.reason}"
        if not answer.failed:
            return "a success terminal answered, not a refusal"
        return f"failure_code {code!r} arrived, want {inputs.refusal_failure_code!r}"


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
    check_dev_ready: bool = False,
) -> HandlerScenarioR9:
    """The production wiring: the registry home, the bus commands, R8's bus lane control and the corpus bus request.

    R9 declares no settings overlay (it holds no secret), so the environment is the process environment as it is. Its
    control lane and its corpus request both read R8's overlay document for the bus, lane and probe tenant: a missing
    tenant_read block is refused by the control inside the run, never by a crash before it.
    """
    runtime = load_runtime()
    settings = load_bus_settings()
    environment = dict(os.environ if env is None else env)  # ONEX_FLAG_EXEMPT
    home = Path(environment[runtime.home_env])
    append_argv = shlex.split(environment.get(runtime.ledger_append_command_env, ""))
    if not append_argv:
        raise RuntimeError(
            f"{runtime.ledger_append_command_env} is not set; the result has no ledger route"
        )
    host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
    r8_settings = load_lane_settings()
    tenant_read = load_tenant_read_overlay(R8_NODE_NAME, environment)

    def control() -> ModelTotals:
        """R8's own bus lane, read once: the proof the bus the corpus answers over is alive."""
        if tenant_read is None:
            raise LaneReadError(NO_R8_TENANT_READ)
        lane = LANE_BUILDERS["bus"](r8_settings, environment, tenant_read)
        return lane.read_totals()

    request: CorpusRequest = (
        build_corpus_request(
            tenant_read,
            command_node=settings.command_node,
            workspace_env=settings.workspace_env,
            environment=environment,
        )
        if tenant_read is not None
        else _no_overlay_request
    )
    return HandlerScenarioR9(
        runtime,
        home=home,
        environment=environment,
        appender=bus_appender(
            CommandLedgerAppender(
                append_argv, runtime.lane, host, runtime.ledger_append_timeout_seconds
            )
        ),
        sink=build_event_sink(
            environment,
            command_env=runtime.bus_emit_command_env,
            journal=home / runtime.event_journal_relpath,
            requested_by=runtime.lane,
            timeout_seconds=runtime.bus_emit_timeout_seconds,
        ),
        terminal_topic=load_terminal_topic(CONTRACT_PATH),
        request=request,
        control=control,
        trigger=trigger,
        check_dev_ready=check_dev_ready,
    )
