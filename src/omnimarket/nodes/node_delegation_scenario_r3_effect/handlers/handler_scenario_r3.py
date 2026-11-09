# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R3, forced escalation from a free rung to a paid rung (OMN-23182, readiness plan 2.2).

One run drives the delegation skill handler with synthetic requests on a large input and rungs-mode probe ladders:

a. a free rung that answers 503 on every call, then a paid rung (a stub with a fixed answer): the free rung shows its attempts
   and each one is typed ``model_unavailable``, the run makes ONE move, the accepted attempt is the paid rung (paid in the
   ladder file) and its bytes equal the fixed answer;
b. the same ladder with the paid rung a real OpenRouter model on the probe key, capped at the contract's ``max_tokens``: the
   accepted attempt reports usage source ``measured`` and a cost above zero. Enabled with the key variable empty, the run is
   red, never skipped;
c. control: both rungs 503 ends ``returned_to_caller`` with a reason from the closed list;
d. control: the same paid rung met by an S (small) input is a skip with cause ``size_band_below_paid`` (OMN-23180's rule,
   decided by the escalation decision node from the contract's ``paid_from_size_band``). The dispatch port does not apply
   that rule to a probe ladder (a probe ladder decides the rung itself), so this control reads the rule's own decision for
   the S input and its measured band, not a ladder run.

Every assertion compares typed fields or bytes of the terminal; no model judges a pass. The result goes to the ledger first
(one STATUS row, the row the green-days reading counts) and then to the bus. The probe key is read from the named variable at
call time only and is never printed, logged, emitted or stored.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.delegation_paid_band import resolve_paid_from_size_band
from omnimarket.handlers.delegation_readiness_overlay import (
    DelegationEffect,
    ModelOpenRouterOverlay,
    OpenRouterOverlayTransport,
    probe_key_present,
)
from omnimarket.handlers.delegation_readiness_probe import (
    LegTransportScope,
    LoopbackStubServers,
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
from omnimarket.handlers.handler_size_band_measurement import measure_size_band
from omnimarket.inference.task_class_authority import resolve_size_band_thresholds
from omnimarket.models.delegation.model_paid_rung_decision import (
    ModelPaidRungDecision,
    ModelPaidRungRequest,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_transitions,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r3_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
PROBE_SCENARIO_PROMPT_TASK = "research"
#: The prefix of the dispatch port's stamp on an attempt that was followed by a move to another rung.
MOVE_STAMP_PREFIX = "transition=move:"

Clock = Callable[[], datetime]
PaidRungDecider = Callable[[ModelPaidRungRequest], ModelPaidRungDecision]


class ModelR3Runtime(ModelReadinessRuntime):
    """The shared runtime keys plus R3's leg b settings."""

    leg_b_enabled: bool
    openrouter_probe_key_env: str = Field(min_length=1)
    openrouter_model_id: str = Field(min_length=1)
    openrouter_max_tokens: int = Field(ge=1)
    openrouter_base_url: str = Field(min_length=1, pattern=r"^https://")

    @property
    def overlay(self) -> ModelOpenRouterOverlay:
        return ModelOpenRouterOverlay(
            probe_key_env=self.openrouter_probe_key_env,
            model_id=self.openrouter_model_id,
            max_tokens=self.openrouter_max_tokens,
            base_url=self.openrouter_base_url,
        )


class ModelScenarioR3RunCommand(BaseModel):
    """One run of scenario R3. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFreeRung(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expect_attempts: int = Field(ge=1)
    expect_failure_class: str = Field(min_length=1)
    #: The kind of move that leaves the free rung.
    expect_move: str = Field(min_length=1)


class ModelPairLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    rungs: tuple[ModelRungScript, ...] = Field(min_length=2, max_length=2)


class ModelLegB(ModelPairLeg):
    #: The rung the real-backend overlay sends to the paid model of the node contract.
    real_backend_id: str = Field(min_length=1)


class ModelSInput(BaseModel):
    """The S (small) input of the skip control: a prompt that must measure below the contract's paid band."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expect_size_band: EnumSizeBand
    #: The cause the paid rung's skip must carry.
    expect_skip_cause: str = Field(min_length=1)


class ModelR3Inputs(BaseModel):
    """The committed ladder file of scenario R3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_marker: str = Field(min_length=8)
    answer: str = Field(min_length=1)
    expected_returned: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expect_size_band: str = Field(min_length=1)
    paid_tiers: tuple[str, ...] = Field(min_length=1)
    end_of_ladder_reasons: tuple[str, ...] = Field(min_length=1)
    free_rung: ModelFreeRung
    leg_a: ModelPairLeg
    leg_b: ModelLegB
    all_down_leg: ModelPairLeg
    s_input: ModelSInput

    def leg_spec(self, name: str, rungs: tuple[ModelRungScript, ...]) -> ModelLegSpec:
        return ModelLegSpec(
            name=name,
            task_type=PROBE_SCENARIO_PROMPT_TASK,
            prompt=self.prompt,
            answer=self.answer,
            rungs=rungs,
        )


def load_inputs(path: Path) -> ModelR3Inputs:
    return ModelR3Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelR3Runtime:
    """The contract's runtime block with the paid rung's model and endpoint read from the overlay file it names."""
    runtime = dict(read_contract(path)["runtime"])  # type: ignore[call-overload]
    overlay_path = NODE_DIR / str(runtime.pop("openrouter_overlay_relpath"))
    runtime.update(yaml.safe_load(overlay_path.read_text(encoding="utf-8")))
    return ModelR3Runtime.model_validate(runtime)


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


class HandlerScenarioR3(ReadinessScenarioBase):
    """Run scenario R3 once, write its STATUS row and emit its result on the bus."""

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
        paid_rung_effect: DelegationEffect | None = None,
        paid_rung_decider: PaidRungDecider | None = None,
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
        assert isinstance(runtime, ModelR3Runtime)
        self._r3 = runtime
        self._inputs_path = inputs_path or NODE_DIR / runtime.inputs_relpath
        self._paid_rung_effect = paid_rung_effect
        self._paid_rung_decider: PaidRungDecider = (
            paid_rung_decider or HandlerEscalationDecision().decide_paid_rung
        )

    def handle(self, request: ModelScenarioR3RunCommand) -> ModelScenarioResult:
        return self.run()

    # -- the scenario ---------------------------------------------------------------------------------------------

    def _run_leg(
        self, spec: ModelLegSpec, *, real_backend_id: str | None = None
    ) -> ModelLegOutcome:
        transport = self._transport_factory()
        # The real paid rung is called only on the real backend; the in-memory backend scripts it as a paid reply.
        if real_backend_id is not None and isinstance(transport, LoopbackStubServers):
            transport = OpenRouterOverlayTransport(
                transport,
                overlay=self._r3.overlay,
                real_backend_id=real_backend_id,
                environment=self._environment,
                effect=self._paid_rung_effect,
            )
        with LegTransportScope(transport) as scoped:
            return run_leg(
                spec,
                scoped,
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

        a = inputs.leg_a
        self._check_forced_escalation(
            check, inputs, "leg_a", self._run_leg(inputs.leg_spec(a.name, a.rungs)), a
        )

        if not self._r3.leg_b_enabled:
            check("leg_b_enabled", False, "leg b is switched off in the contract")
        elif not probe_key_present(self._environment, self._r3.overlay):
            check(
                "leg_b_probe_key_present",
                False,
                f"{self._r3.openrouter_probe_key_env} is empty",
            )
        else:
            check("leg_b_probe_key_present", True)
            b = inputs.leg_b
            outcome_b = self._run_leg(
                inputs.leg_spec(b.name, b.rungs), real_backend_id=b.real_backend_id
            )
            self._check_forced_escalation(check, inputs, "leg_b", outcome_b, b)
            accepted = next((x for x in outcome_b.attempts if x.accepted), None)
            check(
                "leg_b_usage_measured",
                accepted is not None and accepted.usage_source == "measured",
                f"usage_source {accepted.usage_source if accepted else None}",
            )
            check(
                "leg_b_cost_positive",
                accepted is not None and accepted.cost_usd > 0,
                f"cost_usd {accepted.cost_usd if accepted else None}",
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
            "every_down_typed",
            bool(ended.attempts)
            and all(x.failure_class not in (None, "unknown") for x in ended.attempts),
            f"attempt classes {[x.failure_class for x in ended.attempts]}",
        )
        self._check_s_input_skip(check, inputs)
        return "; ".join(problems) or "every assertion held"

    def _check_s_input_skip(
        self, check: Callable[[str, bool, str], None], inputs: ModelR3Inputs
    ) -> None:
        """The S input meets the paid rung as a declared skip, never an answer (the plan's falsifying input, after K3)."""
        s = inputs.s_input
        thresholds = resolve_size_band_thresholds(PROBE_SCENARIO_PROMPT_TASK)
        measured: EnumSizeBand | None = None
        if thresholds is not None:
            measured = measure_size_band(
                task_class=PROBE_SCENARIO_PROMPT_TASK,
                prompt=s.prompt,
                context_pack="",
                sources=(),
                acceptance_criteria=(),
                thresholds=thresholds[0],
                thresholds_reference=thresholds[1],
            ).band
        check(
            "s_input_size_band",
            measured == s.expect_size_band,
            f"band {measured}, want {s.expect_size_band}",
        )
        decision = self._paid_rung_decider(
            ModelPaidRungRequest(
                rung_is_paid=True,
                size_band=measured,
                paid_from_size_band=resolve_paid_from_size_band(
                    PROBE_SCENARIO_PROMPT_TASK
                ),
                transitions=resolve_task_class_transitions(PROBE_SCENARIO_PROMPT_TASK),
            )
        )
        cause = decision.transition.cause.value if decision.transition else None
        check(
            "s_input_paid_rung_skipped",
            not decision.allowed and cause == s.expect_skip_cause,
            f"allowed {decision.allowed}, cause {cause}, want a skip with cause {s.expect_skip_cause}",
        )

    @staticmethod
    def _check_forced_escalation(
        check: Callable[[str, bool, str], None],
        inputs: ModelR3Inputs,
        prefix: str,
        outcome: ModelLegOutcome,
        leg: ModelPairLeg,
    ) -> None:
        free_id, paid_id = leg.rungs[0].backend_id, leg.rungs[1].backend_id
        free = [x for x in outcome.attempts if x.backend_id == free_id]
        rule = inputs.free_rung
        check(
            f"{prefix}_free_rung_retries",
            len(free) == rule.expect_attempts
            and all(x.failure_class == rule.expect_failure_class for x in free),
            f"{len(free)} free attempts with classes {[x.failure_class for x in free]}, "
            f"want {rule.expect_attempts} x {rule.expect_failure_class}",
        )
        moves = [
            x for x in outcome.attempts if MOVE_STAMP_PREFIX in x.acceptance_detail
        ]
        check(
            f"{prefix}_one_move",
            len(moves) == 1
            and moves[0].backend_id == free_id
            and moves[0].acceptance_detail.endswith(f":{rule.expect_move}"),
            f"{len(moves)} moves, status {outcome.status}, cause {outcome.terminal_failure_cause}, "
            f"reason {outcome.terminal_failure_reason}",
        )
        accepted = [x for x in outcome.attempts if x.accepted]
        check(
            f"{prefix}_completed",
            outcome.status == "completed",
            f"status {outcome.status}, cause {outcome.terminal_failure_cause}, "
            f"reason {outcome.terminal_failure_reason}",
        )
        check(
            f"{prefix}_paid_rung_accepted",
            len(accepted) == 1
            and accepted[0].backend_id == paid_id
            and accepted[0].tier in inputs.paid_tiers,
            f"accepted {[(x.backend_id, x.tier) for x in accepted]}, paid tiers {inputs.paid_tiers}",
        )
        check(
            f"{prefix}_size_band",
            bool(outcome.attempts)
            and all(x.size_band == inputs.expect_size_band for x in outcome.attempts),
            f"bands {[x.size_band for x in outcome.attempts]}, want {inputs.expect_size_band}",
        )
        if prefix == "leg_a":
            check(
                f"{prefix}_answer_bytes",
                outcome.final_answer.strip() == inputs.expected_returned.strip(),
                "the returned bytes are not the fixed answer",
            )


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> HandlerScenarioR3:
    """The production wiring: the registry home, the bus commands and the contract's leg backend."""
    runtime = load_runtime()
    built = build_scenario(
        HandlerScenarioR3,
        runtime,
        contract_path=CONTRACT_PATH,
        env=os.environ if env is None else env,
        trigger=trigger,
    )
    assert isinstance(built, HandlerScenarioR3)
    return built
