# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R6, a task longer than 240 seconds completes under a configured 600 second ceiling (OMN-23174, readiness plan 2.2).

One run drives the delegation skill handler with a synthetic summarization request and a rungs-mode probe ladder whose one
stub rung answers after 300 s under its own 450 s deadline, asked for with a 500 s timeout:

1. with a site overlay that sets the summarization ceiling to 600 s the run completes, takes longer than the contract's own
   240 s ceiling, its budget evidence names ceiling 600 and its budget source is the overlay, and the returned bytes carry the
   fixed answer;
2. the same request without the overlay is refused before dispatch: ``failed``, cause ``refused_before_dispatch``, the
   refusal reason ``timeout_exceeds_task_class_ceiling``, no attempt, and the budget source is the contract.

Every assertion compares typed fields or bytes of the terminal; no model judges a pass. The result goes to the ledger first (one
STATUS row, the row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

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
from omnimarket.models.delegation.model_execution_budget_overlay import (
    ModelExecutionBudgetOverlay,
    ModelExecutionBudgetOverride,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r6_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
MILLIS_PER_SECOND = 1000

Clock = Callable[[], datetime]


class ModelScenarioR6RunCommand(BaseModel):
    """One run of scenario R6. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelR6Inputs(BaseModel):
    """The committed inputs file of scenario R6."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(min_length=1)
    #: The task class's own ceiling in the contract; the long run must outlast it.
    contract_ceiling_seconds: int = Field(ge=1)
    #: The ceiling the site overlay sets for the class.
    overlay_ceiling_seconds: int = Field(ge=1)
    requested_timeout_seconds: int = Field(ge=1)
    answer_marker: str = Field(min_length=8)
    answer: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    refusal_reason: str = Field(min_length=1)
    refusal_cause: str = Field(min_length=1)
    rung: ModelRungScript

    def overlay(self) -> ModelExecutionBudgetOverlay:
        return ModelExecutionBudgetOverlay(
            execution_budgets={
                self.task_type: ModelExecutionBudgetOverride(
                    task_class_timeout_ceiling_seconds=self.overlay_ceiling_seconds
                )
            }
        )

    def leg_spec(self, name: str, *, with_overlay: bool) -> ModelLegSpec:
        return ModelLegSpec(
            name=name,
            task_type=self.task_type,
            prompt=self.prompt,
            answer=self.answer,
            rungs=(self.rung,),
            requested_timeout_seconds=self.requested_timeout_seconds,
            isolate_overlay_root=True,
            budget_overlay=self.overlay() if with_overlay else None,
        )


def load_inputs(path: Path) -> ModelR6Inputs:
    return ModelR6Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessRuntime:
    return ModelReadinessRuntime.model_validate(read_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


class HandlerScenarioR6(ReadinessScenarioBase):
    """Run scenario R6 once, write its STATUS row and emit its result on the bus."""

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

    def handle(self, request: ModelScenarioR6RunCommand) -> ModelScenarioResult:
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

        long = self._run_leg(inputs.leg_spec("overlay_long_task", with_overlay=True))
        check(
            "long_completed",
            long.status == "completed",
            f"status {long.status}, cause {long.terminal_failure_cause}, refusal {long.budget_refusal_reason}",
        )
        check(
            "long_duration_over_contract_ceiling",
            long.execution_duration_ms
            > inputs.contract_ceiling_seconds * MILLIS_PER_SECOND,
            f"{long.execution_duration_ms} ms against the contract's {inputs.contract_ceiling_seconds} s ceiling",
        )
        check(
            "long_ceiling_from_overlay",
            long.budget_ceiling_seconds == inputs.overlay_ceiling_seconds,
            f"ceiling {long.budget_ceiling_seconds}, want {inputs.overlay_ceiling_seconds}",
        )
        check(
            "long_budget_source_overlay",
            (long.budget_source or "").startswith("overlay:"),
            f"budget source {long.budget_source}",
        )
        check(
            "long_answer_bytes",
            inputs.answer_marker in long.final_answer,
            "the returned bytes do not carry the fixed answer",
        )

        refused = self._run_leg(
            inputs.leg_spec("no_overlay_refused", with_overlay=False)
        )
        check(
            "refused_failed",
            refused.status == "failed",
            f"status {refused.status}",
        )
        check(
            "refused_before_dispatch",
            refused.terminal_failure_cause == inputs.refusal_cause
            and not refused.attempts,
            f"cause {refused.terminal_failure_cause}, attempts {len(refused.attempts)}",
        )
        check(
            "refused_reason_requested_above_ceiling",
            refused.budget_refusal_reason == inputs.refusal_reason,
            f"refusal reason {refused.budget_refusal_reason}",
        )
        check(
            "refused_request_above_contract_ceiling",
            refused.budget_requested_seconds == inputs.requested_timeout_seconds
            and refused.budget_ceiling_seconds == inputs.contract_ceiling_seconds,
            f"requested {refused.budget_requested_seconds}, ceiling {refused.budget_ceiling_seconds}",
        )
        check(
            "refused_budget_source_contract",
            (refused.budget_source or "").startswith("contract:"),
            f"budget source {refused.budget_source}",
        )
        return "; ".join(problems) or "every assertion held"


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> HandlerScenarioR6:
    """The production wiring: the registry home, the bus commands and the contract's leg backend."""
    runtime = load_runtime()
    built = build_scenario(
        HandlerScenarioR6,
        runtime,
        contract_path=CONTRACT_PATH,
        env=os.environ if env is None else env,  # ONEX_FLAG_EXEMPT
        trigger=trigger,
    )
    assert isinstance(built, HandlerScenarioR6)
    return built
