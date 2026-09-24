# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18928 (K1 of OMN-18925): outcome and content verdict are separate facts.

Before K1 the Market orchestrator stated one thing about a terminal:
``quality_passed`` plus a ``quality_score``. A 429, a provider outage, a
cancelled remote agent and an empty ``choices`` array all left the orchestrator
with ``quality_score=0.0`` -- the same number a model earns by writing a bad
answer. A reader summing quality scores therefore counted every provider outage
as a model that answered badly, which is exactly what the epic says must never
happen.

omnibase_core 0.47.22 (omnibase_core#1730, OMN-18996) added the two typed
fields to the canonical terminal and the invariants that bind them. This file
proves the Market producer now states them on every terminal kind the ticket
names, through the real orchestrator and, for the gate path, the real gate
verdict shape:

========================  ==========================  =================  =======
case                      operational_outcome         content_verdict    score
========================  ==========================  =================  =======
valid raw response        completed                   usable             graded
refusal                   refused                     not_applicable     graded
malformed vs contract     schema_rejected             unusable           graded
truncated / preamble      quality_rejected            unusable           graded
quota (429)               provider_quota              not_applicable     none
provider outage (503)     provider_unavailable        not_applicable     none
timeout                   timeout                     not_applicable     none
cancellation              cancelled                   not_applicable     none
empty ``choices``         inference_failed            not_applicable     none
unconstructible terminal  terminal_construction_...   undetermined       none
========================  ==========================  =================  =======

"none" means the key is ABSENT from the serialized terminal, not ``0.0``.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.models.delegation.wire import (
    EnumDelegationContentVerdict,
    EnumDelegationOperationalOutcome,
    EnumDelegationTerminalFailureCause,
    EnumQualityRuleEnforcement,
    ModelQualityRuleEvaluation,
)
from pydantic import ValidationError

from omnimarket.delegation.reasoning_preamble import UNRESOLVED_PREAMBLE_CHECK_NAME
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.provider_finish_reason import TRUNCATION_CHECK_NAME
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as orchestrator_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    _a2a_operational_outcome,
    _gate_outcome_pair,
    _operational_outcome_for_inference_failure,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = pytest.mark.unit

# Higher than any task class's configured ceiling, so the escalation compute
# answers "ladder exhausted" and the orchestrator emits its FAILED terminal
# instead of another routing intent. The branch under test is the terminal.
_LADDER_EXHAUSTED = 99

_OUTCOME = EnumDelegationOperationalOutcome
_VERDICT = EnumDelegationContentVerdict


def _request(cid: UUID, *, task_type: str = "test") -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _routing_decision(cid: UUID, *, task_type: str = "test") -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type=task_type,
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="http://lab-llm.invalid:8000/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=4096,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed to qwen3-coder-30b.",
    )


def _routed_workflow(cid: UUID) -> HandlerDelegationWorkflow:
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(_routing_decision(cid))
    return handler


def _only_terminal(events: list[object]) -> ModelDelegationResult:
    terminals = [e for e in events if isinstance(e, ModelDelegationResult)]
    assert len(terminals) == 1, f"expected exactly one terminal, got {events!r}"
    return terminals[0]


def _inference_failure_terminal(error_message: str) -> ModelDelegationResult:
    """Drive a provider failure to the orchestrator's FAILED terminal."""
    cid = uuid4()
    handler = _routed_workflow(cid)
    handler.workflows[cid].escalation_count = _LADDER_EXHAUSTED
    events = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="qwen3-coder-30b",
            latency_ms=50,
            error_message=error_message,
        )
    )
    assert handler.workflows[cid].state == EnumDelegationState.FAILED, (
        "precondition: this failure must reach the FAILED terminal, not escalate"
    )
    return _only_terminal(events)


def _gate_terminal(gate: ModelQualityGateResult, cid: UUID) -> ModelDelegationResult:
    """Drive a response through inference to the gate verdict *gate*."""
    handler = _routed_workflow(cid)
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="def test_verify_registration():\n    assert True",
            model_used="qwen3-coder-30b",
            latency_ms=1200,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            llm_call_id="chatcmpl-omn18928",
        )
    )
    handler.workflows[cid].escalation_count = _LADDER_EXHAUSTED
    return _only_terminal(handler.handle_gate_result(gate))


def _assert_no_response_shape(
    terminal: ModelDelegationResult, outcome: EnumDelegationOperationalOutcome
) -> None:
    assert isinstance(terminal, ModelDelegationFailed)
    assert terminal.operational_outcome is outcome
    assert terminal.content_verdict is _VERDICT.NOT_APPLICABLE
    assert terminal.quality_passed is False
    assert terminal.quality_score is None
    # The wire shape a projection and a caller read: no score key at all, so no
    # reader can average a provider outage in as a model that scored zero.
    dumped = terminal.model_dump(mode="json")
    assert "quality_score" not in dumped
    assert dumped["operational_outcome"] == outcome.value
    assert dumped["content_verdict"] == _VERDICT.NOT_APPLICABLE.value


class TestNoProviderResponseIsNeverAQualityScore:
    """Provider-side failures carry an outcome and no score, through the handler."""

    def test_quota_is_provider_quota_with_the_quota_cause(self) -> None:
        terminal = _inference_failure_terminal("HTTP 429: rate limit exceeded")

        _assert_no_response_shape(terminal, _OUTCOME.PROVIDER_QUOTA)
        # Core binds the quota outcome to the observed quota cause.
        assert (
            terminal.terminal_failure_cause
            is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
        )

    def test_provider_outage_is_provider_unavailable(self) -> None:
        terminal = _inference_failure_terminal("HTTP 503: service unavailable")

        _assert_no_response_shape(terminal, _OUTCOME.PROVIDER_UNAVAILABLE)

    def test_timeout_is_timeout(self) -> None:
        terminal = _inference_failure_terminal("request timed out after 240s")

        _assert_no_response_shape(terminal, _OUTCOME.TIMEOUT)

    def test_empty_final_response_is_inference_failed(self) -> None:
        # The live OMN-18223 shape: the vendor metered the call and returned an
        # empty ``choices`` array, so there is no final response to judge.
        terminal = _inference_failure_terminal(
            "provider returned an empty choices array"
        )

        _assert_no_response_shape(terminal, _OUTCOME.INFERENCE_FAILED)


class TestGradedResponsesKeepTheirScoreAndGetAVerdict:
    """A response that reached the gate is graded; the verdict says how."""

    def test_valid_raw_response_is_completed_and_usable(self) -> None:
        # Positive control: an artifact-only valid completion stays usable and
        # completed, and keeps the score it was graded at.
        cid = uuid4()
        terminal = _gate_terminal(
            ModelQualityGateResult(correlation_id=cid, passed=True, quality_score=0.9),
            cid,
        )

        assert isinstance(terminal, ModelDelegationCompleted)
        assert terminal.operational_outcome is _OUTCOME.COMPLETED
        assert terminal.content_verdict is _VERDICT.USABLE
        assert terminal.quality_score == pytest.approx(0.9)

    def test_refusal_is_refused_and_not_applicable(self) -> None:
        cid = uuid4()
        terminal = _gate_terminal(
            ModelQualityGateResult(
                correlation_id=cid,
                passed=False,
                fail_category="fail_heuristic",
                quality_score=0.1,
                failure_reasons=("REFUSAL: detected refusal phrases: i cannot",),
            ),
            cid,
        )

        assert isinstance(terminal, ModelDelegationFailed)
        assert terminal.operational_outcome is _OUTCOME.REFUSED
        assert terminal.content_verdict is _VERDICT.NOT_APPLICABLE
        assert terminal.quality_passed is False

    def test_quality_one_response_behind_a_preamble_is_unusable(self) -> None:
        # The D1 bar: a response that is scratchpad with no answer behind it is
        # not usable, however the rest of the gate would have scored it.
        cid = uuid4()
        terminal = _gate_terminal(
            ModelQualityGateResult(
                correlation_id=cid,
                passed=False,
                fail_category="fail_deterministic",
                quality_score=0.0,
                failure_reasons=("WEAK_OUTPUT: no deliverable behind the preamble",),
                rule_evaluations=(
                    ModelQualityRuleEvaluation(
                        rule=UNRESOLVED_PREAMBLE_CHECK_NAME,
                        enforcement=EnumQualityRuleEnforcement.BLOCKING,
                        passed=False,
                        detail="no deliverable behind the preamble",
                    ),
                ),
            ),
            cid,
        )

        assert isinstance(terminal, ModelDelegationFailed)
        assert terminal.operational_outcome is _OUTCOME.QUALITY_REJECTED
        assert terminal.content_verdict is _VERDICT.UNUSABLE
        assert terminal.quality_score == pytest.approx(0.0)


class TestGateOutcomePair:
    """The derivation itself, one branch per gate floor and authority."""

    @staticmethod
    def _failed(
        *,
        reasons: tuple[str, ...] = ("WEAK_OUTPUT: too short",),
        failed_rule: str | None = None,
    ) -> ModelQualityGateResult:
        evaluations = (
            (
                ModelQualityRuleEvaluation(
                    rule=failed_rule,
                    enforcement=EnumQualityRuleEnforcement.BLOCKING,
                    passed=False,
                    detail=reasons[0],
                ),
            )
            if failed_rule is not None
            else ()
        )
        return ModelQualityGateResult(
            correlation_id=uuid4(),
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.2,
            failure_reasons=reasons,
            rule_evaluations=evaluations,
        )

    def test_malformed_response_against_a_declared_contract_is_schema_rejected(
        self,
    ) -> None:
        pair = _gate_outcome_pair(
            self._failed(reasons=("MALFORMED: $.summary is a required property",)),
            completed=False,
            response_contract_declared=True,
        )

        assert pair == (_OUTCOME.SCHEMA_REJECTED, _VERDICT.UNUSABLE)

    @pytest.mark.parametrize(
        "floor", [TRUNCATION_CHECK_NAME, UNRESOLVED_PREAMBLE_CHECK_NAME]
    )
    def test_a_content_floor_names_the_failure_even_under_a_contract(
        self, floor: str
    ) -> None:
        # The gate evaluates both floors ahead of the contract, so a truncated or
        # scratchpad-only response is a quality rejection, never blamed on the
        # contract the model never reached.
        pair = _gate_outcome_pair(
            self._failed(failed_rule=floor),
            completed=False,
            response_contract_declared=True,
        )

        assert pair == (_OUTCOME.QUALITY_REJECTED, _VERDICT.UNUSABLE)

    def test_a_refusal_without_a_contract_is_refused(self) -> None:
        pair = _gate_outcome_pair(
            self._failed(reasons=("REFUSAL: detected refusal phrases: sorry",)),
            completed=False,
            response_contract_declared=False,
        )

        assert pair == (_OUTCOME.REFUSED, _VERDICT.NOT_APPLICABLE)

    def test_any_other_failed_verdict_is_quality_rejected(self) -> None:
        pair = _gate_outcome_pair(
            self._failed(),
            completed=False,
            response_contract_declared=False,
        )

        assert pair == (_OUTCOME.QUALITY_REJECTED, _VERDICT.UNUSABLE)

    def test_a_completion_is_completed_and_usable(self) -> None:
        pair = _gate_outcome_pair(
            ModelQualityGateResult(
                correlation_id=uuid4(), passed=True, quality_score=0.95
            ),
            completed=True,
            response_contract_declared=True,
        )

        assert pair == (_OUTCOME.COMPLETED, _VERDICT.USABLE)


class TestOperationalOutcomeMaps:
    @pytest.mark.parametrize(
        ("failure_class", "outcome"),
        [
            (EnumDelegationFailureClass.RATE_LIMITED, _OUTCOME.PROVIDER_QUOTA),
            (
                EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                _OUTCOME.PROVIDER_UNAVAILABLE,
            ),
            (EnumDelegationFailureClass.TIMEOUT, _OUTCOME.TIMEOUT),
            (
                EnumDelegationFailureClass.RUNTIME_RESTART_DURING_DELEGATION,
                _OUTCOME.CANCELLED,
            ),
            (EnumDelegationFailureClass.UNKNOWN, _OUTCOME.INFERENCE_FAILED),
            (
                EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
                _OUTCOME.INFERENCE_FAILED,
            ),
        ],
    )
    def test_inference_failure_class(
        self,
        failure_class: EnumDelegationFailureClass,
        outcome: EnumDelegationOperationalOutcome,
    ) -> None:
        assert _operational_outcome_for_inference_failure(failure_class) is outcome

    @pytest.mark.parametrize(
        ("lifecycle", "outcome"),
        [
            (EnumAgentTaskLifecycleType.COMPLETED, _OUTCOME.COMPLETED),
            (EnumAgentTaskLifecycleType.TIMED_OUT, _OUTCOME.TIMEOUT),
            (EnumAgentTaskLifecycleType.CANCELED, _OUTCOME.CANCELLED),
            (EnumAgentTaskLifecycleType.FAILED, _OUTCOME.INFERENCE_FAILED),
        ],
    )
    def test_remote_agent_lifecycle(
        self,
        lifecycle: EnumAgentTaskLifecycleType,
        outcome: EnumDelegationOperationalOutcome,
    ) -> None:
        assert _a2a_operational_outcome(lifecycle) is outcome


class TestConstructionFailureIsTheReservedPair:
    def test_an_unconstructible_terminal_is_construction_failed_and_undetermined(
        self,
    ) -> None:
        cid = uuid4()
        try:
            ModelDelegationCompleted.model_validate({"correlation_id": "not-a-uuid"})
        except ValidationError as exc:
            error = exc
        else:  # pragma: no cover - the payload above can never validate
            pytest.fail("precondition: the malformed payload must not validate")

        terminal = orchestrator_module._unconstructible_terminal(
            _ConstructionInputs(cid),  # type: ignore[arg-type]
            error,
        )

        assert terminal.operational_outcome is _OUTCOME.TERMINAL_CONSTRUCTION_FAILED
        assert terminal.content_verdict is _VERDICT.UNDETERMINED
        assert terminal.quality_score is None
        assert terminal.terminal_failure_reason == "terminal_construction_failed"


class _ConstructionInputs:
    """The fields ``_unconstructible_terminal`` reads, and nothing else."""

    def __init__(self, cid: UUID) -> None:
        self.correlation_id = cid
        self.task_type = "test"
        self.model_used = "qwen3-coder-30b"
        self.endpoint_url = "http://lab-llm.invalid:8000/v1/chat/completions"
        self.content = "def test_x():\n    assert True"
        self.quality_passed = True
        self.quality_score = 0.9
        self.latency_ms = 10
        self.fallback_to_claude = False
        self.completed = True


class TestEveryTerminalSiteStatesThePair:
    """Structural: a new terminal construction site cannot forget either field.

    The case tests above would pass again the moment someone adds a terminal
    construction site that leaves the pair out -- which is how OMN-18223's
    fourth site came to drop the credential. ``TerminalEmissionInputs`` gives
    both fields no default, so a site that omits one fails at construction;
    this pins that every construction in the module passes both by keyword.
    """

    def test_every_terminal_emission_inputs_names_both_fields(self) -> None:
        tree = ast.parse(inspect.getsource(orchestrator_module))
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TerminalEmissionInputs"
        ]
        assert len(sites) >= 4, "precondition: the known construction sites exist"
        for site in sites:
            keywords = {kw.arg for kw in site.keywords}
            assert {"operational_outcome", "content_verdict"} <= keywords, (
                f"TerminalEmissionInputs at line {site.lineno} omits the K1 pair"
            )
