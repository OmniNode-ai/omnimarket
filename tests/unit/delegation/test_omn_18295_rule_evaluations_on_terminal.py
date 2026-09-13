# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's per-rule record reaches the delegation terminal (OMN-18295).

`omnimarket#2512` gave the quality gate a per-rule record: every declared
check's own verdict, the threshold it applied, and whether it was entitled to
veto. It reached the orchestrator, which rendered the BLOCKING failures into a
``deciding_rules=`` fragment inside the free-text terminal reason -- and then
the record itself stopped. The terminal DTO carried no field for it, so the
gateway, which builds its customer-facing read model out of that payload, had
nothing to serve. A customer whose delegation COMPLETED saw no per-rule
evidence at all, because the free-text reason is null on a completed run.

These tests pin the producer half: the record is copied verbatim onto the
terminal, passing rules included, and the acceptance branch can never emit a
terminal that contradicts it.
"""

from __future__ import annotations

import dataclasses
import typing as t

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationResult

from omnimarket.models.delegation.wire.model_quality_gate import (
    EnumQualityRuleEnforcement,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hdw,
)

pytestmark = pytest.mark.unit


class TestOneDefinitionOfTheRecord:
    def test_the_rule_record_is_the_core_wire_type(self) -> None:
        """One wire field must not have two wire contracts.

        The terminal that carries this record is ``ModelDelegationResult``,
        which lives in omnibase_core. A second definition in this repo would
        be a divergence waiting to happen at exactly the boundary that cannot
        tolerate one.
        """
        assert (
            ModelQualityRuleEvaluation.__module__
            == "omnibase_core.models.delegation.wire.model_quality_gate"
        )
        assert (
            EnumQualityRuleEnforcement.__module__
            == "omnibase_core.enums.enum_quality_rule_enforcement"
        )


class TestTheTerminalInputsCarryIt:
    def test_the_emission_inputs_declare_the_field(self) -> None:
        fields = {f.name for f in dataclasses.fields(hdw.TerminalEmissionInputs)}
        assert "rule_evaluations" in fields

    def test_it_defaults_empty_for_a_terminal_no_gate_ran_for(self) -> None:
        """Pre-gate inference failures and agent-lifecycle terminals."""
        default = next(
            f
            for f in dataclasses.fields(hdw.TerminalEmissionInputs)
            if f.name == "rule_evaluations"
        ).default
        assert default == ()


class TestTheWireDtoAcceptsIt:
    @staticmethod
    def _result(**kwargs: t.Any) -> ModelDelegationResult:
        defaults: dict[str, t.Any] = {
            "correlation_id": "1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a",
            "task_type": "summarization",
            "model_used": "glm-4.6",
            "endpoint_url": "https://example.invalid/v1/chat",
            "content": "ok",
            "quality_passed": True,
            "quality_score": 0.9,
            "latency_ms": 5,
            "fallback_to_claude": False,
        }
        defaults.update(kwargs)
        return ModelDelegationResult(**defaults)

    def test_a_scored_miss_rides_a_completed_terminal(self) -> None:
        """The shape ca144d1a-ea03-475f-bc81-650ccfa0495e should have had.

        It scored 0.900 against an 0.800 bar with ``concise`` missed, and was
        terminalised failed anyway. A scored miss moves the score; the bar
        decides; and the terminal must be able to say both at once.
        """
        dumped = self._result(
            rule_evaluations=(
                ModelQualityRuleEvaluation(
                    rule="concise",
                    enforcement=EnumQualityRuleEnforcement.SCORED,
                    passed=False,
                    threshold=250,
                    threshold_unit="words",
                    detail="response is not concise",
                ),
                ModelQualityRuleEvaluation(
                    rule="accurate",
                    enforcement=EnumQualityRuleEnforcement.BLOCKING,
                    passed=True,
                ),
            )
        ).model_dump(mode="json")
        carried = {item["rule"]: item for item in dumped["rule_evaluations"]}
        assert carried["concise"]["threshold"] == 250
        assert carried["accurate"]["passed"] is True

    def test_an_empty_record_is_omitted_from_the_wire(self) -> None:
        """A terminal no gate ran for emits the pre-OMN-18295 payload exactly."""
        assert "rule_evaluations" not in self._result().model_dump(mode="json")


class TestTheRecordStatesWhatHappened:
    def test_a_failed_blocking_rule_may_ride_a_completed_terminal(self) -> None:
        """The case that falsified this change's first design.

        A blocking miss looks like it must be the verdict, and the terminal
        model refused that pairing at first. Two existing proofs broke
        immediately: ``test_judge_unavailable_deterministic_floor_omn13959``
        and ``test_quality_gate_judge_combine_omn13470``. When the judge is
        unreachable the DETERMINISTIC acceptance floor decides, and a run
        whose blocking heuristics failed completes on that floor by declared
        policy.

        So the producer stamps the gate's verdicts verbatim and does not edit
        them into agreement with the outcome. Which authority decided is
        carried separately, by ``score_vs_required_bar`` and the terminal
        reason's ``score_source``. A record edited to agree with the verdict
        is the receipt this ticket exists to remove.
        """
        result = TestTheWireDtoAcceptsIt._result(
            quality_passed=True,
            rule_evaluations=(
                ModelQualityRuleEvaluation(
                    rule="no_obvious_regressions",
                    enforcement=EnumQualityRuleEnforcement.BLOCKING,
                    passed=False,
                    detail="failed no_obvious_regressions",
                ),
            ),
        )
        assert result.quality_passed is True
        assert result.rule_evaluations[0].passed is False

    def test_the_same_rule_cannot_be_recorded_twice(self) -> None:
        """The structural invariant that has no counter-example."""
        with pytest.raises(ValueError, match="at most once"):
            TestTheWireDtoAcceptsIt._result(
                rule_evaluations=(
                    ModelQualityRuleEvaluation(
                        rule="concise",
                        enforcement=EnumQualityRuleEnforcement.SCORED,
                        passed=True,
                    ),
                    ModelQualityRuleEvaluation(
                        rule="concise",
                        enforcement=EnumQualityRuleEnforcement.SCORED,
                        passed=False,
                        detail="response is not concise",
                    ),
                ),
            )
