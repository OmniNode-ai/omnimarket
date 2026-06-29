# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for the canonical PR-review nodes (OMN-13212 / B2).

The bespoke ``node_pr_review_bot`` WorkflowPackage is decomposed into four
canonical nodes; this suite proves:

  * the FSM REDUCER's transition coverage (generated over the contract phase
    sequence) INCLUDING the negative / reject paths (illegal advance-from-terminal,
    3-failure circuit breaker -> FAILED) — per the B2 DoD;
  * the judge-verdict-parse COMPUTE is pure and fail-closed;
  * the github-review EFFECT performs each operation as an EFFECT (events only);
  * the ORCHESTRATOR runs the full pipeline over injected canonical effects and a
    RECORDED-FROM-REAL inference replay (OMN-13498 B1), preserving the
    ``ReviewVerdict`` output shape and the ``pr-review-bot-completed`` terminal
    event. The reviewer-findings and judge-PASS responses replayed for the
    reviewer/judge routes were CAPTURED FROM REAL z.ai GLM (``cloud-glm``,
    ``glm-5.2``) calls resolved through the committed routing contract (see
    ``tests/fixtures/inference_replay/glm_review_findings_two.json`` +
    ``glm_judge_pass.json``). The replay adapter HARD-REJECTS a delegation tier
    name handed in as a ``model_key``, so it cannot mask the
    tier-name-as-model_key regression a hand-written canned fake would (OMN-13470 /
    OMN-13497 ``check-no-faked-boundary``).

The reducer is the canonical REDUCER archetype (typed FSM schema + traverser
exists), so its chains are GENERATED from the phase sequence rather than
hand-authored. The ORCHESTRATOR has no typed-FSM field today; its transition
coverage is hand-authored (§6 option (b)) — see the # hand-authored marker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    ModelFindingEvidence,
    ModelReviewFinding,
)
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_github_review_effect.handlers.handler_github_review_effect import (
    HandlerGithubReviewEffect,
    build_summary_comment,
)
from omnimarket.nodes.node_judge_verdict_parse_compute.handlers.handler_judge_verdict_parse_compute import (
    HandlerJudgeVerdictParseCompute,
    parse_judge_response,
)
from omnimarket.nodes.node_pr_review_fsm_reducer.handlers.handler_pr_review_fsm import (
    HandlerPrReviewFsm,
    advance,
    start_state,
)
from omnimarket.nodes.node_pr_review_fsm_reducer.models.model_pr_review_advance_command import (
    ModelPrReviewAdvanceCommand,
)
from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
    HandlerPrReviewOrchestrator,
)
from omnimarket.nodes.node_pr_review_orchestrator.models.model_pr_review_completed_event import (
    ModelPrReviewCompletedEvent,
)
from omnimarket.review.pr_review_fsm import (
    MAX_CONSECUTIVE_FAILURES,
    TERMINAL_PHASES,
    EnumFsmPhase,
    ModelPrReviewBotState,
    next_phase,
)
from omnimarket.review.pr_review_io import (
    EnumPrVerdict,
    ReviewRequest,
)
from omnimarket.review.pr_review_node_io import (
    EnumGithubReviewOperation,
    ModelGithubReviewCommand,
    ModelGithubReviewResultEvent,
    ModelJudgeParseRequest,
)
from tests.fixtures.inference_replay import RecordedReplayInferenceAdapter


def _make_request(*, dry_run: bool = True) -> ReviewRequest:
    return ReviewRequest(
        correlation_id=uuid4(),
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        reviewer_models=["reviewer-a"],
        judge_model="judge-a",
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _success_edges() -> list[tuple[EnumFsmPhase, EnumFsmPhase]]:
    edges: list[tuple[EnumFsmPhase, EnumFsmPhase]] = []
    cur = EnumFsmPhase.INIT
    while cur != EnumFsmPhase.DONE:
        nxt = next_phase(cur)
        edges.append((cur, nxt))
        cur = nxt
    return edges


# ---------------------------------------------------------------------------
# FSM REDUCER golden chains (generated over all transition edges)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrReviewFsmReducerGoldenChain:
    def test_full_success_chain_init_to_done(self) -> None:
        """Generated chain: every successful edge INIT -> ... -> DONE."""
        state = start_state(_make_request())
        events = []
        while state.current_phase not in TERMINAL_PHASES:
            state, event = advance(state, phase_success=True)
            events.append(event)
        assert state.current_phase == EnumFsmPhase.DONE
        # 7 transitions: INIT->FETCH->REVIEW->POST->WATCH->JUDGE->REPORT->DONE
        assert len(events) == 7
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumFsmPhase.INIT
        assert events[0].to_phase == EnumFsmPhase.FETCH_DIFF
        assert events[-1].to_phase == EnumFsmPhase.DONE

    @pytest.mark.parametrize("edge", _success_edges())
    def test_each_success_edge_advances(
        self, edge: tuple[EnumFsmPhase, EnumFsmPhase]
    ) -> None:
        """Generated: every success edge in the contract sequence is covered."""
        from_phase, to_phase = edge
        state = ModelPrReviewBotState(
            correlation_id=uuid4(),
            pr_number=1,
            repo="o/r",
            current_phase=from_phase,
        )
        new_state, event = advance(state, phase_success=True)
        assert new_state.current_phase == to_phase
        assert event.from_phase == from_phase
        assert event.to_phase == to_phase
        assert event.success is True

    def test_circuit_breaker_after_3_failures(self) -> None:
        """Negative chain: 3 consecutive failures -> FAILED."""
        state = start_state(_make_request())
        state, _ = advance(state, phase_success=True)  # -> FETCH_DIFF
        for i in range(MAX_CONSECUTIVE_FAILURES - 1):
            state, _ = advance(state, phase_success=False, error_message=f"fail {i}")
            assert state.current_phase != EnumFsmPhase.FAILED
        state, event = advance(state, phase_success=False, error_message="fail final")
        assert state.current_phase == EnumFsmPhase.FAILED
        assert event.to_phase == EnumFsmPhase.FAILED
        assert state.consecutive_failures == MAX_CONSECUTIVE_FAILURES

    def test_single_failure_retries_in_place(self) -> None:
        """Negative edge: one failure retries the same phase, counter increments."""
        state = start_state(_make_request())
        state, _ = advance(state, phase_success=True)
        before = state.current_phase
        state, event = advance(state, phase_success=False, error_message="transient")
        assert state.current_phase == before
        assert state.consecutive_failures == 1
        assert event.success is False

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_PHASES))
    def test_advance_from_terminal_rejects(self, terminal: EnumFsmPhase) -> None:
        """Reject path: advancing from any terminal phase raises ValueError."""
        state = ModelPrReviewBotState(
            correlation_id=uuid4(), pr_number=1, repo="o/r", current_phase=terminal
        )
        with pytest.raises(ValueError, match="terminal phase"):
            advance(state, phase_success=True)

    def test_success_resets_failure_counter(self) -> None:
        state = start_state(_make_request())
        state, _ = advance(state, phase_success=True)
        state, _ = advance(state, phase_success=False, error_message="blip")
        assert state.consecutive_failures == 1
        state, _ = advance(state, phase_success=True)
        assert state.consecutive_failures == 0

    async def test_reducer_handle_emits_state_projection(self) -> None:
        """The REDUCER handler folds an advance event into a state projection."""
        handler = HandlerPrReviewFsm()
        state = start_state(_make_request())
        command = ModelPrReviewAdvanceCommand(state=state, phase_success=True)
        envelope: ModelEventEnvelope[ModelPrReviewAdvanceCommand] = ModelEventEnvelope(
            payload=command,
            correlation_id=state.correlation_id,
            event_type="onex.evt.omnimarket.pr-review-bot-phase-advance.v1",
        )
        output = await handler.handle(envelope)
        assert output.node_kind == EnumNodeKind.REDUCER
        assert len(output.projections) == 1
        projected = output.projections[0]
        assert isinstance(projected, ModelPrReviewBotState)
        assert projected.current_phase == EnumFsmPhase.FETCH_DIFF
        assert output.events == ()
        assert output.intents == ()
        assert output.result is None


# ---------------------------------------------------------------------------
# Judge verdict parse COMPUTE
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeVerdictParseCompute:
    def test_pass_verdict(self) -> None:
        result = parse_judge_response('{"verdict": "PASS", "reasoning": "fixed"}')
        assert result.passed is True
        assert result.reasoning == "fixed"

    def test_fail_verdict(self) -> None:
        result = parse_judge_response(
            '{"verdict": "FAIL", "reasoning": "not addressed"}'
        )
        assert result.passed is False

    def test_markdown_fenced_json(self) -> None:
        raw = '```json\n{"verdict": "PASS", "reasoning": "ok"}\n```'
        assert parse_judge_response(raw).passed is True

    def test_malformed_json_fails_closed(self) -> None:
        result = parse_judge_response("not json at all")
        assert result.passed is False
        assert "malformed" in result.reasoning.lower()

    def test_unknown_verdict_fails_closed(self) -> None:
        result = parse_judge_response('{"verdict": "MAYBE", "reasoning": "x"}')
        assert result.passed is False

    async def test_compute_handler_returns_result(self) -> None:
        handler = HandlerJudgeVerdictParseCompute()
        output = await handler.handle(
            ModelJudgeParseRequest(
                correlation_id=uuid4(),
                raw_text='{"verdict": "PASS", "reasoning": "ok"}',
            )
        )
        assert output.node_kind == EnumNodeKind.COMPUTE
        assert output.result is not None
        assert output.result.passed is True
        assert output.events == ()


# ---------------------------------------------------------------------------
# GitHub review EFFECT
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGithubReviewEffect:
    async def test_post_threads_dry_run_emits_event(self) -> None:
        handler = HandlerGithubReviewEffect()
        output = await handler.handle(
            ModelGithubReviewCommand(
                correlation_id=uuid4(),
                operation=EnumGithubReviewOperation.POST_THREADS,
                repo="o/r",
                pr_number=1,
                dry_run=True,
                findings=(),
            )
        )
        assert output.node_kind == EnumNodeKind.EFFECT
        assert len(output.events) == 1
        event = output.events[0]
        assert isinstance(event, ModelGithubReviewResultEvent)
        assert event.operation is EnumGithubReviewOperation.POST_THREADS
        # EFFECT must not emit intents/projections/result.
        assert output.intents == ()
        assert output.projections == ()
        assert output.result is None

    async def test_post_report_dry_run_no_comment_id(self) -> None:
        from omnimarket.review.pr_review_io import ReviewVerdict

        verdict = ReviewVerdict(
            correlation_id=uuid4(),
            pr_number=1,
            repo="o/r",
            verdict=EnumPrVerdict.CLEAN,
            total_findings=0,
            threads_posted=0,
            threads_verified_pass=0,
            threads_verified_fail=0,
            threads_pending=0,
            judge_model_used="judge-a",
            duration_ms=10,
            completed_at=datetime.now(tz=UTC),
        )
        handler = HandlerGithubReviewEffect()
        output = await handler.handle(
            ModelGithubReviewCommand(
                correlation_id=uuid4(),
                operation=EnumGithubReviewOperation.POST_REPORT,
                repo="o/r",
                pr_number=1,
                dry_run=True,
                verdict=verdict,
            )
        )
        assert output.events[0].report_comment_id is None

    def test_build_summary_comment_renders_verdict(self) -> None:
        from omnimarket.review.pr_review_io import ReviewVerdict

        verdict = ReviewVerdict(
            correlation_id=uuid4(),
            pr_number=1,
            repo="o/r",
            verdict=EnumPrVerdict.BLOCKING_ISSUE,
            total_findings=2,
            threads_posted=2,
            threads_verified_pass=0,
            threads_verified_fail=1,
            threads_pending=1,
            judge_model_used="judge-a",
            duration_ms=1000,
            completed_at=datetime.now(tz=UTC),
        )
        body = build_summary_comment(verdict, (), ())
        assert "BLOCKED" in body
        assert "Merge is blocked" in body


# ---------------------------------------------------------------------------
# ORCHESTRATOR — full pipeline over injected effects + fake inference
# ---------------------------------------------------------------------------


# The reviewer / judge routes the pr-review orchestrator dispatches to
# (ReviewRequest.reviewer_models / judge_model in _make_request). Mapping the
# replay fixtures by route key keeps a tier name from ever resolving as a route.
_REVIEWER_ROUTE = "reviewer-a"
_JUDGE_ROUTE = "judge-a"

# The REAL recorded GLM responses (replayed, never canned): the reviewer route
# replays a live 2-finding hostile review, the judge route replays a live PASS
# verdict. Both captured from cloud-glm calls resolved through the routing
# contract. The 2-finding fixture yields exactly two findings (assert below).
_RECORDED_REVIEWER_FINDINGS = 2


def _recorded_review_adapter() -> RecordedReplayInferenceAdapter:
    """Replay the REAL recorded reviewer findings + judge PASS by route key."""
    return RecordedReplayInferenceAdapter(
        route_fixtures={
            _REVIEWER_ROUTE: "glm_review_findings_two.json",
            _JUDGE_ROUTE: "glm_judge_pass.json",
        },
    )


class _RecordedGithubDiffEffect(HandlerGithubDiffEffect):
    """Returns a fixed diff without touching GitHub (a NON-inference boundary).

    The github-diff EFFECT is not the platform's inference/routing/dispatch egress
    that the no-faked-boundary gate guards; this stub supplies a deterministic diff
    so the orchestrator can drive the real recorded inference replay.
    """

    async def handle(self, request):  # type: ignore[override, no-untyped-def]
        from omnibase_core.models.dispatch.model_handler_output import (
            ModelHandlerOutput,
        )

        from omnimarket.nodes.node_github_diff_effect.models.model_github_diff import (
            ModelGithubDiffResolvedEvent,
        )

        event = ModelGithubDiffResolvedEvent(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            content="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
            content_chars=42,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="fake-github-diff",
            events=(event,),
        )


@pytest.mark.unit
class TestPrReviewOrchestratorGoldenChain:
    async def test_full_pipeline_dry_run_completes_done(self) -> None:
        """hand-authored: FSM schema not yet on orchestrators (verdict b).

        Replay-equivalent golden chain: start command -> resolved diff -> findings
        -> threads (dry_run pending) -> watch -> judge -> report -> completed event.
        Output preserves the ReviewVerdict shape + terminal phase.
        """
        orchestrator = HandlerPrReviewOrchestrator(
            inference_adapter=_recorded_review_adapter(),
            github_diff_effect=_RecordedGithubDiffEffect(),
            github_review_effect=HandlerGithubReviewEffect(),
        )
        request = _make_request(dry_run=True)
        output = await orchestrator.handle(request)

        assert output.node_kind == EnumNodeKind.ORCHESTRATOR
        # ORCHESTRATOR: events only.
        assert output.projections == ()
        assert output.result is None
        assert len(output.events) == 1
        completed = output.events[0]
        assert isinstance(completed, ModelPrReviewCompletedEvent)
        assert completed.final_phase == EnumFsmPhase.DONE
        assert completed.verdict.correlation_id == request.correlation_id
        assert completed.verdict.pr_number == request.pr_number
        # dry_run posts no threads, so findings noted but no verified-fail -> risks_noted
        assert completed.verdict.verdict in {
            EnumPrVerdict.RISKS_NOTED,
            EnumPrVerdict.CLEAN,
        }
        assert completed.verdict.total_findings == _RECORDED_REVIEWER_FINDINGS

    async def test_diff_failure_fails_closed_blocking(self) -> None:
        """A failure in the pipeline drives the FSM to FAILED + BLOCKING_ISSUE."""

        class _BoomDiff(HandlerGithubDiffEffect):
            async def handle(self, request):  # type: ignore[override, no-untyped-def]
                raise RuntimeError("diff resolution exploded")

        orchestrator = HandlerPrReviewOrchestrator(
            inference_adapter=_recorded_review_adapter(),
            github_diff_effect=_BoomDiff(),
            github_review_effect=HandlerGithubReviewEffect(),
        )
        output = await orchestrator.handle(_make_request(dry_run=True))
        completed = output.events[0]
        assert isinstance(completed, ModelPrReviewCompletedEvent)
        assert completed.final_phase == EnumFsmPhase.FAILED
        assert completed.verdict.verdict == EnumPrVerdict.BLOCKING_ISSUE


def _make_finding() -> ModelReviewFinding:
    return ModelReviewFinding(
        id=uuid4(),
        category=EnumFindingCategory.LOGIC_ERROR,
        severity=EnumFindingSeverity.MAJOR,
        title="x",
        description="y",
        evidence=ModelFindingEvidence(file_path="x.py"),
        confidence=EnumReviewConfidence.HIGH,
        source_model="reviewer-a",
    )


@pytest.mark.unit
def test_review_finding_adapts_from_shared_model() -> None:
    """ReviewFinding.from_model_review_finding preserves the shared finding shape."""
    from omnimarket.review.pr_review_io import ReviewFinding

    base = _make_finding()
    pr = ReviewFinding.from_model_review_finding(base)
    assert pr.id == base.id
    assert pr.severity == base.severity
    assert pr.evidence.file_path == "x.py"
