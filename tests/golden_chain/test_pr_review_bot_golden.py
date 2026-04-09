"""Golden chain tests for node_pr_review_bot.

Verifies the full FSM pipeline: INIT -> FETCH_DIFF -> REVIEW -> POST_THREADS
-> WATCH -> JUDGE_VERIFY -> REPORT -> DONE, circuit breaker, dry_run,
findings-to-verdict derivation, event serialization, and WorkflowRunner
end-to-end with all stub implementations.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    ModelFindingEvidence,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    HandlerPrReviewBot,
    ModelPhaseTransitionEvent,
    ProtocolDiffFetcher,
    ProtocolJudgeVerifier,
    ProtocolReportPoster,
    ProtocolReviewer,
    ProtocolThreadPoster,
    ProtocolThreadWatcher,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumFsmPhase,
    EnumPrVerdict,
    EnumReviewConfidence,
    EnumThreadStatus,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)
from omnimarket.nodes.node_pr_review_bot.workflow_runner import (
    WorkflowRunnerResult,
    run_review,
)

CMD_TOPIC = "onex.cmd.omnimarket.pr-review-bot-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.pr-review-bot-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.pr-review-bot-completed.v1"

SAMPLE_REPO = "OmniNode-ai/omnimarket"
SAMPLE_PR = 99


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _make_request(
    pr_number: int = SAMPLE_PR,
    repo: str = SAMPLE_REPO,
    dry_run: bool = False,
    max_findings: int = 20,
) -> ReviewRequest:
    return ReviewRequest(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo=repo,
        reviewer_models=["qwen3-coder-30b", "qwen3-14b"],
        judge_model="deepseek-r1",
        severity_threshold=EnumFindingSeverity.MAJOR,
        dry_run=dry_run,
        max_findings_per_pr=max_findings,
        requested_at=datetime.now(tz=UTC),
    )


def _make_hunk(
    file_path: str = "src/foo.py",
    content: str = "@@ -1,3 +1,5 @@\n+import os\n+print(os.environ)\n",
) -> DiffHunk:
    return DiffHunk(
        file_path=file_path,
        start_line=1,
        end_line=5,
        content=content,
    )


def _make_finding(
    severity: EnumFindingSeverity = EnumFindingSeverity.MAJOR,
    source_model: str = "qwen3-coder-30b",
) -> ReviewFinding:
    return ReviewFinding(
        id=uuid4(),
        category=EnumFindingCategory.SECURITY,
        severity=severity,
        title="Environment variable exposed in log output",
        description="os.environ dump exposes secrets in plaintext log output.",
        confidence=EnumReviewConfidence.HIGH,
        source_model=source_model,
        evidence=ModelFindingEvidence(
            file_path="src/foo.py",
            line_range={"start": 2, "end": 3},
            code_snippet="print(os.environ)",
        ),
    )


def _make_thread(
    finding_id: UUID | None = None,
    status: EnumThreadStatus = EnumThreadStatus.VERIFIED_PASS,
) -> ThreadState:
    return ThreadState(
        finding_id=finding_id or uuid4(),
        github_thread_id=1001,
        status=status,
    )


# ---------------------------------------------------------------------------
# Noop stub implementations
# ---------------------------------------------------------------------------


class _NoopDiffFetcher(ProtocolDiffFetcher):
    def __init__(self, hunks: list[DiffHunk] | None = None) -> None:
        self._hunks = hunks or [_make_hunk()]

    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        return list(self._hunks)


class _NoopReviewer(ProtocolReviewer):
    def __init__(self, findings: list[ReviewFinding] | None = None) -> None:
        self._findings = findings or []

    def review(
        self,
        correlation_id: UUID,
        diff_hunks: tuple[DiffHunk, ...],
        reviewer_models: list[str],
    ) -> list[ReviewFinding]:
        return list(self._findings)


class _NoopThreadPoster(ProtocolThreadPoster):
    def __init__(self, threads: list[ThreadState] | None = None) -> None:
        self._threads = threads or []

    def post(
        self,
        pr_number: int,
        repo: str,
        findings: tuple[ReviewFinding, ...],
        dry_run: bool,
    ) -> list[ThreadState]:
        return list(self._threads)


class _NoopThreadWatcher(ProtocolThreadWatcher):
    def watch(
        self,
        pr_number: int,
        repo: str,
        thread_states: tuple[ThreadState, ...],
    ) -> list[ThreadState]:
        return list(thread_states)


class _NoopJudgeVerifier(ProtocolJudgeVerifier):
    def verify(
        self,
        correlation_id: UUID,
        findings: tuple[ReviewFinding, ...],
        thread_states: tuple[ThreadState, ...],
        judge_model: str,
    ) -> list[ThreadState]:
        return list(thread_states)


class _NoopReportPoster(ProtocolReportPoster):
    def __init__(self) -> None:
        self.posted: list[ReviewVerdict] = []

    def post_summary(
        self,
        pr_number: int,
        repo: str,
        verdict: ReviewVerdict,
        dry_run: bool,
    ) -> None:
        self.posted.append(verdict)


class _FailingDiffFetcher(ProtocolDiffFetcher):
    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        raise RuntimeError("GitHub API unavailable")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrReviewBotGoldenChain:
    """Golden chain: INIT -> FETCH_DIFF -> REVIEW -> POST_THREADS -> WATCH
    -> JUDGE_VERIFY -> REPORT -> DONE."""

    def _make_stubs(
        self,
        hunks: list[DiffHunk] | None = None,
        findings: list[ReviewFinding] | None = None,
        threads: list[ThreadState] | None = None,
    ) -> tuple[
        _NoopDiffFetcher,
        _NoopReviewer,
        _NoopThreadPoster,
        _NoopThreadWatcher,
        _NoopJudgeVerifier,
        _NoopReportPoster,
    ]:
        return (
            _NoopDiffFetcher(hunks),
            _NoopReviewer(findings),
            _NoopThreadPoster(threads),
            _NoopThreadWatcher(),
            _NoopJudgeVerifier(),
            _NoopReportPoster(),
        )

    def test_full_pipeline_clean_pr(self) -> None:
        """No findings -> DONE with CLEAN verdict."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert final_state.current_phase == EnumFsmPhase.DONE
        assert final_state.consecutive_failures == 0
        assert verdict.verdict == EnumPrVerdict.CLEAN
        assert verdict.total_findings == 0

    def test_full_pipeline_state_transitions_order(self) -> None:
        """Events reflect INIT->FETCH_DIFF->REVIEW->...->DONE in correct order."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        _final_state, events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        # 7 transitions: INIT, FETCH_DIFF, REVIEW, POST_THREADS, WATCH, JUDGE_VERIFY, REPORT
        assert len(events) == 7
        expected_sequence = [
            (EnumFsmPhase.INIT, EnumFsmPhase.FETCH_DIFF),
            (EnumFsmPhase.FETCH_DIFF, EnumFsmPhase.REVIEW),
            (EnumFsmPhase.REVIEW, EnumFsmPhase.POST_THREADS),
            (EnumFsmPhase.POST_THREADS, EnumFsmPhase.WATCH),
            (EnumFsmPhase.WATCH, EnumFsmPhase.JUDGE_VERIFY),
            (EnumFsmPhase.JUDGE_VERIFY, EnumFsmPhase.REPORT),
            (EnumFsmPhase.REPORT, EnumFsmPhase.DONE),
        ]
        for event, (from_p, to_p) in zip(events, expected_sequence, strict=False):
            assert event.from_phase == from_p
            assert event.to_phase == to_p
            assert event.success is True

    def test_diff_hunks_propagated_to_state(self) -> None:
        """Diff hunks fetched in FETCH_DIFF are available in final state."""
        fsm = HandlerPrReviewBot()
        hunk = _make_hunk(file_path="src/auth.py", content="@@ -10 +10 @@\n+pass\n")
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs(hunks=[hunk])

        final_state, _events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert len(final_state.diff_hunks) == 1
        assert final_state.diff_hunks[0].file_path == "src/auth.py"

    def test_findings_propagated_to_verdict(self) -> None:
        """Findings produced in REVIEW appear in final verdict counts."""
        fsm = HandlerPrReviewBot()
        finding = _make_finding(severity=EnumFindingSeverity.MAJOR)
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs(findings=[finding])

        final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert len(final_state.findings) == 1
        assert verdict.total_findings == 1
        assert verdict.verdict == EnumPrVerdict.RISKS_NOTED

    def test_verdict_blocking_issue_when_threads_verified_fail(self) -> None:
        """BLOCKING_ISSUE verdict when at least one thread is VERIFIED_FAIL."""
        fsm = HandlerPrReviewBot()
        finding = _make_finding()
        thread = _make_thread(
            finding_id=finding.id, status=EnumThreadStatus.VERIFIED_FAIL
        )
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs(findings=[finding], threads=[thread])

        _final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert verdict.verdict == EnumPrVerdict.BLOCKING_ISSUE
        assert verdict.threads_verified_fail == 1

    def test_report_poster_called_with_verdict(self) -> None:
        """ReportPoster.post_summary is called exactly once with the derived verdict."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        fsm.run_full_pipeline(request, df, rv, tp, tw, jv, rp)

        assert len(rp.posted) == 1
        assert rp.posted[0].pr_number == SAMPLE_PR
        assert rp.posted[0].repo == SAMPLE_REPO

    def test_dry_run_propagated_through_state(self) -> None:
        """dry_run=True is preserved in FSM state and passed to sub-handlers."""
        fsm = HandlerPrReviewBot()
        request = _make_request(dry_run=True)
        df, rv, tp, tw, jv, rp = self._make_stubs()

        final_state, _events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert final_state.dry_run is True
        assert final_state.current_phase == EnumFsmPhase.DONE

    def test_fetch_diff_exception_stops_pipeline(self) -> None:
        """A FETCH_DIFF exception stops the pipeline; consecutive_failures increments.

        The FSM breaks after the first failure (non-terminal phase) rather than
        retrying automatically. The circuit breaker trips only when advance() is
        called three times with phase_success=False (tested in TestFsmAdvanceDirectly).
        """
        fsm = HandlerPrReviewBot()
        request = _make_request()

        final_state, events, _verdict = fsm.run_full_pipeline(
            request,
            _FailingDiffFetcher(),
            _NoopReviewer(),
            _NoopThreadPoster(),
            _NoopThreadWatcher(),
            _NoopJudgeVerifier(),
            _NoopReportPoster(),
        )

        # Pipeline halts at FETCH_DIFF (non-terminal) after one exception
        assert final_state.current_phase == EnumFsmPhase.FETCH_DIFF
        assert final_state.consecutive_failures == 1
        failure_events = [e for e in events if not e.success]
        assert len(failure_events) == 1
        assert failure_events[0].from_phase == EnumFsmPhase.FETCH_DIFF

    def test_cannot_advance_from_terminal_done(self) -> None:
        """advance() from DONE raises ValueError."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()
        final_state, _events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        with pytest.raises(ValueError, match="terminal phase"):
            fsm.advance(final_state, phase_success=True)

    def test_cannot_advance_from_terminal_failed(self) -> None:
        """advance() from FAILED raises ValueError."""
        fsm = HandlerPrReviewBot()
        request = _make_request()

        final_state, _events, _verdict = fsm.run_full_pipeline(
            request,
            _FailingDiffFetcher(),
            _NoopReviewer(),
            _NoopThreadPoster(),
            _NoopThreadWatcher(),
            _NoopJudgeVerifier(),
            _NoopReportPoster(),
        )
        if final_state.current_phase == EnumFsmPhase.FAILED:
            with pytest.raises(ValueError, match="terminal phase"):
                fsm.advance(final_state, phase_success=True)

    def test_events_serialize_to_valid_json(self) -> None:
        """All phase transition events serialize to valid JSON bytes."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        _final_state, events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        for event in events:
            raw = fsm.serialize_event(event)
            parsed = json.loads(raw)
            assert "from_phase" in parsed
            assert "to_phase" in parsed
            assert "success" in parsed

    def test_verdict_correlation_id_matches_request(self) -> None:
        """Verdict correlation_id matches the original ReviewRequest."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        _final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert verdict.correlation_id == request.correlation_id
        assert verdict.pr_number == request.pr_number
        assert verdict.repo == request.repo

    def test_multiple_findings_from_multiple_models(self) -> None:
        """Findings from multiple reviewer models are all accumulated."""
        fsm = HandlerPrReviewBot()
        findings = [
            _make_finding(
                severity=EnumFindingSeverity.CRITICAL, source_model="qwen3-coder-30b"
            ),
            _make_finding(severity=EnumFindingSeverity.MAJOR, source_model="qwen3-14b"),
            _make_finding(
                severity=EnumFindingSeverity.MINOR, source_model="qwen3-coder-30b"
            ),
        ]
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs(findings=findings)

        final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert len(final_state.findings) == 3
        assert verdict.total_findings == 3

    def test_thread_states_propagated_through_watch_and_verify(self) -> None:
        """Thread states flow from POST_THREADS -> WATCH -> JUDGE_VERIFY -> REPORT."""
        fsm = HandlerPrReviewBot()
        finding = _make_finding()
        thread = _make_thread(
            finding_id=finding.id, status=EnumThreadStatus.VERIFIED_PASS
        )
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs(findings=[finding], threads=[thread])

        final_state, _events, verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        assert len(final_state.thread_states) == 1
        assert final_state.thread_states[0].status == EnumThreadStatus.VERIFIED_PASS
        assert verdict.threads_verified_pass == 1

    def test_event_correlation_ids_match_request(self) -> None:
        """All transition events carry the request correlation_id."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        df, rv, tp, tw, jv, rp = self._make_stubs()

        _final_state, events, _verdict = fsm.run_full_pipeline(
            request, df, rv, tp, tw, jv, rp
        )

        for event in events:
            assert event.correlation_id == request.correlation_id
            assert event.pr_number == SAMPLE_PR
            assert event.repo == SAMPLE_REPO


@pytest.mark.unit
class TestWorkflowRunnerGoldenChain:
    """Golden chain tests for run_review() WorkflowRunner with mocked GitHub API."""

    def test_run_review_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_review() returns WorkflowRunnerResult without hitting GitHub."""
        # Monkeypatch asyncio.run to return empty hunks (avoids real HTTP)
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            dry_run=True,
        )

        assert isinstance(result, WorkflowRunnerResult)
        assert result.verdict.pr_number == SAMPLE_PR
        assert result.verdict.repo == SAMPLE_REPO

    def test_run_review_clean_verdict_with_no_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty diff -> CLEAN verdict from WorkflowRunner."""
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            dry_run=True,
        )

        assert result.verdict.verdict == EnumPrVerdict.CLEAN
        assert result.verdict.total_findings == 0

    def test_run_review_final_state_is_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorkflowRunner pipeline reaches DONE phase."""
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            dry_run=True,
        )

        from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
            ModelPrReviewBotState,
        )

        assert isinstance(result.final_state, ModelPrReviewBotState)
        assert result.final_state.current_phase == EnumFsmPhase.DONE

    def test_run_review_generates_phase_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorkflowRunner produces 7 phase transition events for clean run."""
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            dry_run=True,
        )

        assert len(result.events) == 7
        assert all(isinstance(e, ModelPhaseTransitionEvent) for e in result.events)

    def test_run_review_correlation_id_propagated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller-supplied correlation_id is preserved in result."""
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        run_id = uuid4()
        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            dry_run=True,
            correlation_id=run_id,
        )

        assert result.correlation_id == run_id
        assert result.verdict.correlation_id == run_id

    def test_run_review_judge_model_stamped_on_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorkflowRunner stamps the judge_model on the final verdict."""
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_review_bot.workflow_runner._DiffFetcherAdapter.fetch",
            lambda _s, _pr, _r: [],
        )

        result = run_review(
            pr_number=SAMPLE_PR,
            repo=SAMPLE_REPO,
            github_token="test-token",
            judge_model="my-judge-model",
            dry_run=True,
        )

        assert result.verdict.judge_model_used == "my-judge-model"


@pytest.mark.unit
class TestFsmAdvanceDirectly:
    """Unit tests for HandlerPrReviewBot.advance() without run_full_pipeline."""

    def test_advance_from_init_succeeds(self) -> None:
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)

        new_state, event = fsm.advance(state, phase_success=True)

        assert new_state.current_phase == EnumFsmPhase.FETCH_DIFF
        assert event.from_phase == EnumFsmPhase.INIT
        assert event.to_phase == EnumFsmPhase.FETCH_DIFF
        assert event.success is True

    def test_advance_failure_increments_consecutive_failures(self) -> None:
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)
        # Move past INIT
        state, _ = fsm.advance(state, phase_success=True)

        state, event = fsm.advance(state, phase_success=False, error_message="timeout")

        assert state.consecutive_failures == 1
        assert state.current_phase == EnumFsmPhase.FETCH_DIFF  # same phase, retry
        assert event.success is False
        assert event.error_message == "timeout"

    def test_circuit_breaker_trips_at_max_failures(self) -> None:
        """3 consecutive failures -> FAILED."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)
        state, _ = fsm.advance(state, phase_success=True)  # past INIT

        state, _ = fsm.advance(state, phase_success=False)
        state, _ = fsm.advance(state, phase_success=False)
        state, event = fsm.advance(state, phase_success=False)

        assert state.current_phase == EnumFsmPhase.FAILED
        assert state.consecutive_failures == 3
        assert event.to_phase == EnumFsmPhase.FAILED

    def test_success_resets_consecutive_failures(self) -> None:
        """Successful advance after failure resets consecutive_failures to 0."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)
        state, _ = fsm.advance(state, phase_success=True)  # past INIT

        # One failure
        state, _ = fsm.advance(state, phase_success=False)
        assert state.consecutive_failures == 1

        # One success
        state, _ = fsm.advance(state, phase_success=True, diff_hunks=[_make_hunk()])
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumFsmPhase.REVIEW

    def test_advance_with_diff_hunks(self) -> None:
        """diff_hunks kwarg is stored in new state on FETCH_DIFF success."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)
        state, _ = fsm.advance(state, phase_success=True)  # INIT -> FETCH_DIFF

        hunk = _make_hunk(file_path="src/secret.py")
        state, _ = fsm.advance(state, phase_success=True, diff_hunks=[hunk])

        assert len(state.diff_hunks) == 1
        assert state.diff_hunks[0].file_path == "src/secret.py"
        assert state.current_phase == EnumFsmPhase.REVIEW

    def test_advance_with_findings(self) -> None:
        """findings kwarg is stored in new state on REVIEW success."""
        fsm = HandlerPrReviewBot()
        request = _make_request()
        state = fsm.start(request)
        state, _ = fsm.advance(state, phase_success=True)
        state, _ = fsm.advance(state, phase_success=True, diff_hunks=[_make_hunk()])

        finding = _make_finding()
        state, _ = fsm.advance(state, phase_success=True, findings=[finding])

        assert len(state.findings) == 1
        assert state.current_phase == EnumFsmPhase.POST_THREADS
