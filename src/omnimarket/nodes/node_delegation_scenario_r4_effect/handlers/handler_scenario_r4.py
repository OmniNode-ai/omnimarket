# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerScenarioR4 is built by the node CLI factory (build_runner) with the contract runtime block, not routed by handler_routing or imported by name
"""Scenario R4, every rung fails and time is shared fairly (OMN-23173, readiness plan 2.2).

One run drives the delegation skill handler with synthetic requests and rungs-mode probe ladders:

1. a rung stopped (nothing listening), a rung killed mid-call and a rung slowed past its deadline, each followed by a healthy
   rung: the run completes, walks both rungs, types the failed attempt (a killed rung is not ``unknown``, OMN-21313), ends
   inside the class ceiling, and the returned bytes carry the fixed answer;
2. a ladder with every rung down: the run ends ``failed``, cause ``returned_to_caller``, a reason from the closed list
   (OMN-23165) and ``savings_usd`` 0;
3. a slowed rung, then a rung whose credential is absent, then a healthy one: the absent rung is never selected and holds no
   share of the run's time (OMN-23170), so the walk is the slow rung then the healthy one, and the third rung answers.

Every assertion compares typed fields or bytes of the terminal; no model judges a pass. The result goes to the ledger first (one
STATUS row, the row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.delegation_readiness_probe import (
    LegTransportScope,
    ModelLegOutcome,
    ModelLegSpec,
    ModelRungScript,
    ProtocolLegTransport,
    run_leg,
)
from omnimarket.handlers.delegation_readiness_scenario import (
    ModelReadinessRuntime,
    ReadinessScenarioBase,
    build_scenario,
    read_contract,
)
from omnimarket.handlers.delegation_readiness_scenario import (
    load_schedule as load_schedule_from,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r4_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
PROBE_SCENARIO_PROMPT_TASK = "research"

Clock = Callable[[], datetime]


class ModelScenarioR4RunCommand(BaseModel):
    """One run of scenario R4. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRecoverLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The typed failure class the first (failed) rung's attempt must carry.
    expect_failure_class: str = Field(min_length=1)
    rungs: tuple[ModelRungScript, ...] = Field(min_length=2)


class ModelAllDownLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    rungs: tuple[ModelRungScript, ...] = Field(min_length=2)


class ModelR4Inputs(BaseModel):
    """The committed ladder file of scenario R4."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_ceiling_ms: int = Field(ge=1)
    answer_marker: str = Field(min_length=8)
    answer: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    end_of_ladder_reasons: tuple[str, ...] = Field(min_length=1)
    recover_legs: tuple[ModelRecoverLeg, ...] = Field(min_length=3)
    all_down_leg: ModelAllDownLeg
    credential_absent_leg: ModelRecoverLeg

    @model_validator(mode="after")
    def _credential_absent_leg_shape(self) -> ModelR4Inputs:
        """The leg must declare an absent rung and end on a rung whose credential resolves (the one that answers)."""
        rungs = self.credential_absent_leg.rungs
        if all(rung.credential_resolved for rung in rungs):
            raise ValueError(
                "credential_absent_leg declares no rung with an absent credential"
            )
        if not rungs[-1].credential_resolved:
            raise ValueError(
                "credential_absent_leg must end on a rung whose credential resolves"
            )
        return self

    def leg_spec(self, name: str, rungs: tuple[ModelRungScript, ...]) -> ModelLegSpec:
        return ModelLegSpec(
            name=name,
            task_type=PROBE_SCENARIO_PROMPT_TASK,
            prompt=self.prompt,
            answer=self.answer,
            rungs=rungs,
        )


def load_inputs(path: Path) -> ModelR4Inputs:
    return ModelR4Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessRuntime:
    return ModelReadinessRuntime.model_validate(read_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


class HandlerScenarioR4(ReadinessScenarioBase):
    """Run scenario R4 once, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelReadinessRuntime,
        *,
        home: Path,
        environment: Mapping[str, str],
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str,
        transport_factory: Callable[[], ProtocolLegTransport],
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
        inputs_path: Path | None = None,
    ) -> None:
        super().__init__(
            runtime,
            home=home,
            environment=environment,
            appender=appender,
            sink=sink,
            terminal_topic=terminal_topic,
            transport_factory=transport_factory,
            trigger=trigger,
            clock=clock,
        )
        self._inputs_path = inputs_path or NODE_DIR / runtime.inputs_relpath

    def handle(self, request: ModelScenarioR4RunCommand) -> ModelScenarioResult:
        return self.run()

    # -- the scenario ---------------------------------------------------------------------------------------------

    def _run_leg(self, spec: ModelLegSpec) -> ModelLegOutcome:
        with LegTransportScope(self._transport_factory()) as transport:
            return run_leg(
                spec,
                transport,
                work_dir=self.work_dir,
                tenant_slug=self._runtime.probe_tenant_slug,
            )

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
        marker = inputs.answer_marker

        for leg in (*inputs.recover_legs, inputs.credential_absent_leg):
            outcome = self._run_leg(inputs.leg_spec(leg.name, leg.rungs))
            healthy = leg.rungs[-1].backend_id
            first = outcome.attempts[0] if outcome.attempts else None
            check(
                f"{leg.name}_completed",
                outcome.status == "completed",
                f"status {outcome.status}, cause {outcome.terminal_failure_cause}",
            )
            check(
                f"{leg.name}_walked_the_reachable_rungs",
                outcome.walked
                == tuple(
                    rung.backend_id for rung in leg.rungs if rung.credential_resolved
                ),
                f"walked {outcome.walked}",
            )
            check(
                f"{leg.name}_typed",
                first is not None
                and first.failure_class == leg.expect_failure_class
                and first.failure_class != "unknown",
                f"first attempt failure_class {first.failure_class if first else None}, want {leg.expect_failure_class}",
            )
            check(
                f"{leg.name}_healthy_accepted",
                bool(outcome.attempts)
                and outcome.attempts[-1].backend_id == healthy
                and outcome.attempts[-1].accepted,
                "the healthy rung was not the accepted attempt",
            )
            check(
                f"{leg.name}_inside_ceiling",
                0 < outcome.execution_duration_ms <= inputs.class_ceiling_ms,
                f"{outcome.execution_duration_ms} ms against a {inputs.class_ceiling_ms} ms ceiling",
            )
            check(
                f"{leg.name}_answer_bytes",
                marker in outcome.final_answer,
                "the returned bytes do not carry the fixed answer",
            )
            absent_rungs = tuple(
                rung.backend_id for rung in leg.rungs if not rung.credential_resolved
            )
            if absent_rungs:
                check(
                    f"{leg.name}_absent_rung_not_attempted",
                    not set(absent_rungs) & set(outcome.walked),
                    f"an attempt was made on a rung whose credential is absent: {outcome.walked}",
                )

        down = inputs.all_down_leg
        ended = self._run_leg(inputs.leg_spec(down.name, down.rungs))
        check("every_down_failed", ended.status == "failed", f"status {ended.status}")
        check(
            "every_down_returned_to_caller",
            ended.terminal_failure_cause == "returned_to_caller",
            f"cause {ended.terminal_failure_cause}",
        )
        check(
            "every_down_reason_in_closed_list",
            ended.terminal_failure_reason in inputs.end_of_ladder_reasons,
            f"reason {ended.terminal_failure_reason}",
        )
        check(
            "every_down_savings_zero",
            ended.cost_savings_usd == 0.0,
            f"savings {ended.cost_savings_usd}",
        )
        check(
            "every_down_typed",
            bool(ended.attempts)
            and all(a.failure_class not in (None, "unknown") for a in ended.attempts),
            f"attempt classes {[a.failure_class for a in ended.attempts]}",
        )
        return "; ".join(problems) or "every assertion held"


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> HandlerScenarioR4:
    """The production wiring: the registry home, the bus commands and the contract's leg backend."""
    runtime = load_runtime()
    built = build_scenario(
        HandlerScenarioR4,
        runtime,
        contract_path=CONTRACT_PATH,
        env=os.environ if env is None else env,  # ONEX_FLAG_EXEMPT
        trigger=trigger,
    )
    assert isinstance(built, HandlerScenarioR4)
    return built
