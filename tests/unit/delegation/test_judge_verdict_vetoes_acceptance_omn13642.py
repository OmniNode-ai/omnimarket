# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13642: the LLM-judge verdict is a co-required acceptance authority.

OMN-13470 combined the judge adequacy SCORE into the verifiable-class quality
gate as a weighted mean (0.6 deterministic + 0.4 judge). That made the judge a
score nudge, not a gate: on a class whose ``required_bar`` is low enough (``test``
/ ``validator_generation`` at 0.8), a perfect deterministic floor (fraction 1.0)
lifts a FAIL judge score over the bar — e.g. judge 0.55 -> combined 0.82 >= 0.80 ->
ACCEPTED, even though the judge VERDICT is ``fail``. The deterministic floor masked
the judge FAIL.

This suite proves the OMN-13642 fix: acceptance is now ``deterministic_gate AND
judge_verdict``. A ``FAIL`` verdict VETOES acceptance on the verifiable path even
when the combined score clears the bar, while the deterministic hard floor still
hard-blocks refusals/empties BEFORE any judge consideration, and a ``PASS`` verdict
keeps the combined-score acceptance unchanged.

The judge EFFECT is dogfooded (OMN-13470 doctrine): the verdict is computed by the
REAL ``HandlerJudgeAdequacy`` over a recorded-from-real GLM response replay
(``RecordedJudgeReplayAdapter``), pinned to the concrete model id — never a
hand-rolled fake bridge. The end-to-end class drives the REAL orchestrator FSM ->
routing reducer -> inference effect -> quality gate -> terminal chain; only the
outbound httpx provider call is patched (the same substitution boundary the
OMN-13409/OMN-13479 suites use).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from pydantic import BaseModel

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from tests.fixtures.judge_inference import RecordedJudgeReplayAdapter

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# A clean delegated unit test: a single fenced artifact that compiles, carries the
# @pytest.mark.unit marker, and has no prose outside the block -> the ``test`` DoD
# deterministic floor (compiles_without_errors / final_artifact_only /
# uses_pytest_mark_unit) FULLY passes, so the deterministic fraction is 1.0 and the
# judge supplies the only adequacy signal. This is the masking scenario: a strong
# floor + a FAIL judge.
_GOOD_TEST_ARTIFACT = (
    "```python\n"
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.mark.unit\n"
    "def test_add() -> None:\n"
    "    assert 1 + 1 == 2\n"
    "```"
)
_REFUSAL = "I cannot help with that request."

# ``test`` required_bar is 0.8 (task_class_contracts.v1.yaml). The combine weights
# are 0.6 deterministic + 0.4 judge, so with a deterministic fraction of 1.0 a
# judge score of 0.55 -> combined 0.82, which CLEARS the 0.8 bar. The verdict for a
# 0.55 score is FAIL (rubric pass_min 0.80 / borderline_min 0.60).
_FAIL_JUDGE_SCORE = 0.55
_PASS_JUDGE_SCORE = 0.95
_TEST_REQUIRED_BAR = 0.8


def _task_class_dod(task_class: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    dod = contract["task_classes"][task_class]["definition_of_done"]
    return tuple(dod.get("deterministic", ())), tuple(dod.get("heuristic", ()))


def _test_gate_input(content: str) -> ModelQualityGateInput:
    det, heur = _task_class_dod("test")
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="test",
        llm_response_content=content,
        dod_deterministic=det,
        dod_heuristic=heur,
    )


# ---------------------------------------------------------------------------
# Pure-reducer veto unit tests (no inference at all)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeVerdictVetoUnit:
    def test_score_only_without_verdict_masks_fail(self) -> None:
        """Control: WITHOUT the verdict, a FAIL score clears the bar and is accepted.

        This is exactly the masking the OMN-13642 veto closes. The combined score
        is at or above the ``test`` required_bar, so the score-only logic accepts a
        candidate the judge actually FAILED.
        """
        result = quality_gate_delta(
            _test_gate_input(_GOOD_TEST_ARTIFACT),
            judge_adequacy_score=_FAIL_JUDGE_SCORE,
        )
        assert result.passed is True
        assert result.score_source == "combined"
        assert result.quality_score >= _TEST_REQUIRED_BAR

    def test_judge_fail_vetoes_despite_score_over_bar(self) -> None:
        """OMN-13642 core: a FAIL verdict vetoes acceptance even over the bar."""
        result = quality_gate_delta(
            _test_gate_input(_GOOD_TEST_ARTIFACT),
            judge_adequacy_score=_FAIL_JUDGE_SCORE,
            judge_verdict=EnumDelegationJudgeVerdict.FAIL,
        )
        assert result.passed is False
        assert result.fail_category == "fail_heuristic"
        assert result.fallback_recommended is True
        # The combined score is recorded for telemetry and STILL clears the bar —
        # proving the rejection is the verdict veto, not a score drop.
        assert result.score_source == "combined"
        assert result.quality_score >= _TEST_REQUIRED_BAR
        assert any("JUDGE_FAIL" in r for r in result.failure_reasons)

    def test_judge_pass_accepts(self) -> None:
        """A PASS verdict keeps the combined-score acceptance unchanged."""
        result = quality_gate_delta(
            _test_gate_input(_GOOD_TEST_ARTIFACT),
            judge_adequacy_score=_PASS_JUDGE_SCORE,
            judge_verdict=EnumDelegationJudgeVerdict.PASS,
        )
        assert result.passed is True
        assert result.fail_category == "pass"
        assert result.score_source == "combined"
        assert result.quality_score >= _TEST_REQUIRED_BAR

    def test_borderline_verdict_does_not_veto(self) -> None:
        """Only FAIL vetoes; BORDERLINE keeps the combined-score behavior."""
        result = quality_gate_delta(
            _test_gate_input(_GOOD_TEST_ARTIFACT),
            judge_adequacy_score=0.70,
            judge_verdict=EnumDelegationJudgeVerdict.BORDERLINE,
        )
        assert result.passed is True
        assert result.score_source == "combined"

    def test_judge_fail_cannot_override_deterministic_hardfloor(self) -> None:
        """A refusal still hard-blocks on the deterministic floor BEFORE the veto.

        The refusal verdict path must read ``fail_deterministic`` (the hard floor),
        not the ``fail_heuristic`` judge veto — the floor is evaluated first and a
        refusal never reaches the verifiable-acceptance branch.
        """
        result = quality_gate_delta(
            _test_gate_input(_REFUSAL),
            judge_adequacy_score=_FAIL_JUDGE_SCORE,
            judge_verdict=EnumDelegationJudgeVerdict.FAIL,
        )
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"
        assert result.score_source != "combined"


# ---------------------------------------------------------------------------
# handle_async over the REAL judge (recorded-from-real GLM replay)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeVerdictThreadsThroughHandleAsync:
    """``handle_async`` runs the real judge EFFECT and threads its VERDICT to delta.

    The judge verdict is computed by the real ``HandlerJudgeAdequacy`` parsing a
    recorded-from-real GLM response (concrete model id pinned by the replay). The
    FAIL replay drives a ``fail`` verdict; the PASS replay a ``pass`` verdict.
    """

    def _intent(self, content: str) -> ModelQualityGateIntent:
        return ModelQualityGateIntent(payload=_test_gate_input(content))

    @pytest.mark.asyncio
    async def test_fail_verdict_vetoes_accept_in_handle_async(self) -> None:
        handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(
                inference_bridge=RecordedJudgeReplayAdapter(
                    "glm_code_adequacy_fail.json"
                )
            )
        )
        output = await handler.handle_async(self._intent(_GOOD_TEST_ARTIFACT))
        result = next(e for e in output.events if isinstance(e, ModelQualityGateResult))
        verdicts = [
            e for e in output.events if isinstance(e, ModelDelegationJudgeVerdictEvent)
        ]
        assert verdicts, "the judge verdict event must be emitted"
        assert verdicts[0].verdict is EnumDelegationJudgeVerdict.FAIL
        # The judge FAIL vetoes acceptance even though the combined score cleared
        # the bar — the published gate result the orchestrator consumes is NOT
        # accepted.
        assert result.passed is False
        assert result.score_source == "combined"
        assert result.quality_score >= _TEST_REQUIRED_BAR

    @pytest.mark.asyncio
    async def test_pass_verdict_accepts_in_handle_async(self) -> None:
        handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(
                inference_bridge=RecordedJudgeReplayAdapter(
                    "glm_code_adequacy_pass.json"
                )
            )
        )
        output = await handler.handle_async(self._intent(_GOOD_TEST_ARTIFACT))
        result = next(e for e in output.events if isinstance(e, ModelQualityGateResult))
        verdicts = [
            e for e in output.events if isinstance(e, ModelDelegationJudgeVerdictEvent)
        ]
        assert verdicts[0].verdict is EnumDelegationJudgeVerdict.PASS
        assert result.passed is True
        assert result.score_source == "combined"


# ---------------------------------------------------------------------------
# Real-dispatch-path end-to-end: the verdict reaches handle_gate_result
# ---------------------------------------------------------------------------

# Self-contained bifrost contract: ``test`` routes (tier_order [local, ...]) to a
# local backend declaring the ``test`` capability + a COMPLETE verbatim endpoint
# URL, so routing resolves host-independently (CI has no ~/.omninode overlay). The
# JUDGE backend (cloud-glm) is resolved separately by the judge adapter, not from
# this delegation contract.
_BIFROST_TEST = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    '    endpoint_url: "http://test-testclass:8000/v1/chat/completions"\n'
    '    model_name: "Qwen3.6-35B-A3B"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [test, code_generation, code_review, refactor, research]\n"
    "routing_rules:\n"
    '  - rule_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"\n'
    "    priority: 10\n"
    "    task_class: test\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [test]\n"
    "    backend_ids: [local-coder]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "ffffffff-ffff-4fff-8fff-ffffffffffff"\n'
    "default_backends:\n"
    "  - local-coder\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)


def _reset_routing_cache() -> None:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

    _h._config = None
    _h._load_bifrost_endpoints.cache_clear()


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    response.raise_for_status.return_value = None
    return response


def _terminal_topics(terminal_events: list[BaseModel]) -> list[str | None]:
    return [
        getattr(e, "topic", None)
        for e in terminal_events
        if isinstance(e, ModelDelegationEvent)
    ]


@pytest.mark.unit
class TestJudgeVerdictVetoRealDispatchPath:
    """Drive the FULL chain for ``test`` and prove the verdict reaches the terminal.

    Every hop is the REAL handler; only the outbound httpx call is patched. The
    gate hop runs ``handle_async`` over the FAIL judge replay, so the verdict veto
    fires and ``handle_gate_result`` must NOT emit ``delegation-completed`` even
    though the combined score cleared the bar.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        _reset_routing_cache()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_TEST, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _reset_routing_cache()

    async def _run_full_chain(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
        *,
        fixture_name: str,
    ) -> tuple[ModelQualityGateResult, list[BaseModel]]:
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(
                inference_bridge=RecordedJudgeReplayAdapter(fixture_name)
            )
        )

        routing_intents = workflow.handle_delegation_request(request)
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        decision = routing_handler.handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(_GOOD_TEST_ARTIFACT)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])

        gate_intents = workflow.handle_inference_response(response)
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        gate_output = await gate_handler.handle_async(gate_intents[0])
        gate_result = next(
            e for e in gate_output.events if isinstance(e, ModelQualityGateResult)
        )
        assert any(
            isinstance(e, ModelDelegationJudgeVerdictEvent) for e in gate_output.events
        ), "the judge verdict event must be emitted (combine active)"

        terminal_events = workflow.handle_gate_result(gate_result)
        return gate_result, terminal_events

    @pytest.mark.asyncio
    async def test_fail_verdict_not_completed_over_dispatch_path(self) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        request = ModelDelegationRequest(
            prompt="Write a unit test for add(a, b).",
            task_type="test",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )
        gate_result, terminal_events = await self._run_full_chain(
            workflow, request, fixture_name="glm_code_adequacy_fail.json"
        )

        # The gate result the orchestrator consumed carries the veto.
        assert gate_result.passed is False
        assert gate_result.score_source == "combined"
        assert gate_result.quality_score >= _TEST_REQUIRED_BAR
        # The verdict reached handle_gate_result: NOT completed.
        assert TOPIC_ID_DELEGATION_COMPLETED not in _terminal_topics(terminal_events)
        wf = workflow.workflows[request.correlation_id]
        assert wf.state != EnumDelegationState.COMPLETED

    @pytest.mark.asyncio
    async def test_pass_verdict_completes_over_dispatch_path(self) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        request = ModelDelegationRequest(
            prompt="Write a unit test for add(a, b).",
            task_type="test",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )
        gate_result, terminal_events = await self._run_full_chain(
            workflow, request, fixture_name="glm_code_adequacy_pass.json"
        )

        assert gate_result.passed is True
        assert gate_result.score_source == "combined"
        assert TOPIC_ID_DELEGATION_COMPLETED in _terminal_topics(terminal_events)
        wf = workflow.workflows[request.correlation_id]
        assert wf.state == EnumDelegationState.COMPLETED
