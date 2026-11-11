# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerScenarioR7 is built by the node CLI factory (build_runner) with the contract runtime block, not routed by handler_routing or imported by name
"""Scenario R7, the receipt and the cost name the answering backend and host (OMN-23696, readiness plan 2.2).

One run drives the delegation skill handler with a synthetic summarization request and a rungs-mode probe ladder of two
rungs: the first rung answers with a 503 and charges nothing, the second answers and charges a scripted cost. The terminal's
receipt must name the second rung's backend id, the host that served it and the cost the rung was scripted to charge; the
failed first attempt must name the first rung and its host, and the terminal's cost must equal the attempts' costs added.

Every assertion compares typed fields or bytes of the terminal; no model judges a pass. The result goes to the ledger first (one
STATUS row, the row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
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
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r7_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"

Clock = Callable[[], datetime]


class ModelScenarioR7RunCommand(BaseModel):
    """One run of scenario R7. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelR7Inputs(BaseModel):
    """The committed inputs file of scenario R7."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_name: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    answer_marker: str = Field(min_length=8)
    answer: str = Field(min_length=1)
    #: The host the scripted transport reports; on the loopback backend the runtime's loopback host is expected instead.
    inmemory_host: str = Field(min_length=1)
    #: How far the terminal's cost may differ from the attempts' costs added (floating point sums).
    cost_tolerance_usd: float = Field(gt=0)
    failing_rung: ModelRungScript
    answering_rung: ModelRungScript

    def leg_spec(self) -> ModelLegSpec:
        return ModelLegSpec(
            name=self.leg_name,
            task_type=self.task_type,
            prompt=self.prompt,
            answer=self.answer,
            rungs=(self.failing_rung, self.answering_rung),
        )

    def expected_host(self, runtime: ModelReadinessRuntime) -> str:
        """The host the receipt must name: the loopback host on the loopback backend, else the scripted inmemory host."""
        if runtime.leg_backend == "loopback":
            return runtime.loopback_host
        return self.inmemory_host


def load_inputs(path: Path) -> ModelR7Inputs:
    return ModelR7Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessRuntime:
    return ModelReadinessRuntime.model_validate(read_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


class HandlerScenarioR7(ReadinessScenarioBase):
    """Run scenario R7 once, write its STATUS row and emit its result on the bus."""

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

    def handle(self, request: ModelScenarioR7RunCommand) -> ModelScenarioResult:
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

        outcome = self._run_leg(inputs.leg_spec())
        check(
            "leg_completed",
            outcome.status == "completed",
            f"status {outcome.status}, cause {outcome.terminal_failure_cause}",
        )
        check(
            "walked_failing_then_answering",
            outcome.walked
            == (
                inputs.failing_rung.backend_id,
                inputs.answering_rung.backend_id,
            ),
            f"walked {outcome.walked}",
        )
        failed = outcome.attempts[0] if outcome.attempts else None
        check(
            "failed_attempt_names_failing_rung",
            failed is not None
            and failed.backend_id == inputs.failing_rung.backend_id
            and not failed.accepted
            and failed.cost_usd == 0,
            f"first attempt {failed.backend_id if failed else None}, accepted {failed.accepted if failed else None}, cost {failed.cost_usd if failed else None}",
        )
        accepted = [a for a in outcome.attempts if a.accepted]
        check(
            "accepted_attempt_names_answering_backend",
            len(accepted) == 1
            and accepted[0].backend_id == inputs.answering_rung.backend_id,
            f"accepted {[(a.backend_id) for a in accepted]}",
        )
        expected_host = inputs.expected_host(self._runtime)
        check(
            "accepted_attempt_names_serving_host",
            bool(accepted) and accepted[0].host == expected_host,
            f"host {accepted[0].host if accepted else None}, want {expected_host}",
        )
        check(
            "failed_attempt_names_serving_host",
            failed is not None and failed.host == expected_host,
            f"host {failed.host if failed else None}, want {expected_host}",
        )
        check(
            "accepted_attempt_cost_positive",
            bool(accepted) and accepted[0].cost_usd > 0,
            f"cost {accepted[0].cost_usd if accepted else None}",
        )
        check(
            "terminal_cost_equals_attempts",
            abs(outcome.cost_usd - sum(a.cost_usd for a in outcome.attempts))
            < inputs.cost_tolerance_usd,
            f"terminal {outcome.cost_usd}, attempts {sum(a.cost_usd for a in outcome.attempts)}",
        )
        check(
            "answer_bytes",
            inputs.answer_marker in outcome.final_answer,
            "the returned bytes do not carry the fixed answer",
        )
        if self._runtime.leg_backend == "inmemory":
            check(
                "accepted_attempt_cost_scripted",
                bool(accepted)
                and Decimal(str(accepted[0].cost_usd))
                == inputs.answering_rung.scripted_cost_usd,
                f"cost {accepted[0].cost_usd if accepted else None}, want {inputs.answering_rung.scripted_cost_usd}",
            )
        return "; ".join(problems) or "every assertion held"


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> HandlerScenarioR7:
    """The production wiring: the registry home, the bus commands and the contract's leg backend."""
    runtime = load_runtime()
    built = build_scenario(
        HandlerScenarioR7,
        runtime,
        contract_path=CONTRACT_PATH,
        env=os.environ if env is None else env,  # ONEX_FLAG_EXEMPT
        trigger=trigger,
    )
    assert isinstance(built, HandlerScenarioR7)
    return built
