# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A terminal that will not validate must still terminate the run (OMN-18978).

Live finding, correlation ``e379a4b9-8fbc-4408-b277-07ec32ac1876``: five rungs
ran and five models answered inside 37 seconds, the quality gate refused every
one at 0.373 against a bar of 0.800, and then the runtime logged

    ValidationError: 1 validation error for ModelDelegationFailed
    Value error, rule_evaluations must record each rule at most once

at the exact second the outcome was decided. Nothing was published. The caller
sat for a further 196 seconds until its own handler budget expired and a
SYNTHESIZED timeout terminal arrived carrying ``attempts: []`` -- so the receipt
reported a run that never dispatched, for a run that had dispatched five times
and been decided.

The duplicate itself is fixed at the merge (see the criteria-union change in the
same pull request). This module is about the OTHER half, which is the one that
makes the failure silent: the outcome existed and only its CARRIER failed, and
the builder had no answer for that. Any future rejected field reproduces the
same 240-second silence, so the guard is written against terminal CONSTRUCTION
rather than against this one field.

WHY THE INPUT HERE IS HAND-BUILT AND ILLEGAL. With the merge fixed, the gate can
no longer produce a duplicated rule set, so driving this through the gate would
prove nothing about the guard. The inputs below carry the duplicate directly,
which is exactly the contract the guard owes its callers: whatever reaches it,
a terminal comes back.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_quality_rule_enforcement import (
    EnumQualityRuleEnforcement,
)
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from omnibase_core.models.delegation.wire.model_quality_gate import (
    ModelQualityRuleEvaluation,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    TERMINAL_CONSTRUCTION_FAILED_REASON,
    HandlerDelegationWorkflow,
    TerminalEmissionInputs,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)

pytestmark = pytest.mark.unit

#: The live run's own budget. The synthesized timeout arrived at 240s; a guard
#: that answered in 239 would technically pass and help nobody, so the bar here
#: is "immediately", not "before the budget".
_SECONDS_A_CALLER_SHOULD_NOT_WAIT = 5.0

_DUPLICATED_RULE = "covers_edge_cases"


def _duplicated_rule_evaluations() -> tuple[ModelQualityRuleEvaluation, ...]:
    """The shape the wire DTO refuses: one rule recorded twice.

    Reproduced as the live run produced it -- once blocking, from the caller's
    `--criteria`, and once scored, from the task class's own definition of done.
    """
    return (
        ModelQualityRuleEvaluation(
            rule=_DUPLICATED_RULE,
            enforcement=EnumQualityRuleEnforcement.BLOCKING,
            passed=False,
            detail="from the caller's acceptance criteria",
        ),
        ModelQualityRuleEvaluation(
            rule=_DUPLICATED_RULE,
            enforcement=EnumQualityRuleEnforcement.SCORED,
            passed=False,
            detail="from the task class's declared definition of done",
        ),
    )


def _inputs(cid: UUID, *, completed: bool = False) -> TerminalEmissionInputs:
    return TerminalEmissionInputs(
        completed=completed,
        correlation_id=cid,
        task_type="test",
        model_used="qwen3.8-27b",
        endpoint_url="http://local.test:8000/v1/chat/completions",
        content="the answer the five rungs actually produced",
        quality_passed=completed,
        quality_score=0.373,
        latency_ms=37_000,
        prompt_tokens=400,
        completion_tokens=120,
        total_tokens=520,
        fallback_to_claude=False,
        failure_reason="quality gate refused every rung",
        tokens_to_compliance=520,
        compliance_attempts=5,
        cost_tier_name="local",
        premium_counterfactual=None,
        escalation_count=4,
        escalation_history=(),
        terminal_failure_reason=None,
        routing_tiers_hash=None,
        escalation_config_hash=None,
        attempts_count=5,
        model_name="qwen3.8-27b",
        session_id=None,
        quality_gates_checked=["covers_edge_cases"],
        quality_gates_failed=["covers_edge_cases"],
        llm_call_id="chatcmpl-omn18978",
        context_pack_hash="",
        prior_attempt_cost_usd=0.0,
        prior_attempt_prompt_tokens=0,
        prior_attempt_completion_tokens=0,
        rule_evaluations=_duplicated_rule_evaluations(),
    )


class TestARejectedTerminalStillTerminates:
    """AC2. The guard's whole contract, stated three ways."""

    def test_a_terminal_comes_back_and_comes_back_at_once(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        started = time.monotonic()
        events = handler._emit_terminal(_inputs(cid))
        elapsed = time.monotonic() - started

        terminals = [e for e in events if isinstance(e, ModelDelegationResult)]
        assert len(terminals) == 1, (
            "an outcome that was decided must still reach its caller; the live "
            "run published nothing at all here"
        )
        assert elapsed < _SECONDS_A_CALLER_SHOULD_NOT_WAIT, (
            f"the terminal took {elapsed:.1f}s; the defect this closes was a "
            "caller waiting out a 240-second budget"
        )

    def test_it_is_typed_failed_and_carries_the_run(self) -> None:
        cid = uuid4()
        terminal = HandlerDelegationWorkflow(workflows={})._emit_terminal(_inputs(cid))[
            0
        ]

        assert isinstance(terminal, ModelDelegationFailed)
        assert terminal.correlation_id == cid, (
            "a terminal nobody can match to a run is not a terminal"
        )
        assert terminal.content == "the answer the five rungs actually produced", (
            "the answer is not the thing that failed to validate; dropping it "
            "would lose work the run already paid for"
        )

    def test_the_reason_names_the_validation_error_and_the_workflow(self) -> None:
        cid = uuid4()
        terminal = HandlerDelegationWorkflow(workflows={})._emit_terminal(_inputs(cid))[
            0
        ]

        assert terminal.terminal_failure_reason == (
            TERMINAL_CONSTRUCTION_FAILED_REASON
        ), "a stable token, so these are countable without parsing prose"
        assert str(cid) in terminal.failure_reason, (
            "the reason must name the workflow it happened to"
        )
        assert "rule_evaluations" in terminal.failure_reason, (
            "the reason must name what was rejected, or the next reader "
            "repeats this entire diagnosis"
        )
        assert "\\n" not in terminal.failure_reason, (
            "a multi-line pydantic message makes the field unreadable on a "
            "receipt and unparseable in a log line"
        )

    def test_a_decided_pass_degrades_to_failed_rather_than_claiming_a_pass(
        self,
    ) -> None:
        """The honest half of the degradation.

        A terminal whose evidence fields could not be constructed cannot claim
        it was graded and passed, so the guard reports FAILED even where the
        run had been decided COMPLETED. The answer itself still rides along.
        """
        terminal = HandlerDelegationWorkflow(workflows={})._emit_terminal(
            _inputs(uuid4(), completed=True)
        )[0]

        assert isinstance(terminal, ModelDelegationFailed)
        assert terminal.quality_passed is False


class TestTheGuardDoesNotFireOnAValidTerminal:
    """Positive control. A guard that always fired would pass every test above.

    Without this, replacing the builder body with an unconditional degraded
    terminal would look like a working fix.
    """

    def test_a_unique_rule_set_builds_the_real_terminal(self) -> None:
        cid = uuid4()
        inputs = _inputs(cid)
        healthy = TerminalEmissionInputs(
            **{
                **inputs.__dict__,
                "rule_evaluations": (_duplicated_rule_evaluations()[0],),
            }
        )

        terminal = HandlerDelegationWorkflow(workflows={})._emit_terminal(healthy)[0]

        assert terminal.terminal_failure_reason != (TERMINAL_CONSTRUCTION_FAILED_REASON)
        assert len(terminal.rule_evaluations) == 1, (
            "the real terminal carries its evidence; only the degraded one drops it"
        )
        assert terminal.escalation_count == 4, "and every other field the run resolved"
