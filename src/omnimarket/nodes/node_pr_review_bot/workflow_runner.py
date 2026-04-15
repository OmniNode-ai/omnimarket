# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WorkflowRunner for node_pr_review_bot.

Wires the FSM handler (HandlerPrReviewBot) with all concrete sub-handler
implementations. Provides stub/placeholder implementations for ThreadPoster,
ThreadWatcher, JudgeVerifier, and ReportPoster — the concrete handlers for
those are implemented in parallel PRs (OMN-7969, OMN-7970, OMN-7971, OMN-7972).

Entry point: run_review(pr_number, repo, github_token)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_pr_review_bot.adapter_github_bridge import (
    AdapterGitHubBridge,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_diff_fetcher import (
    DiffFetcherConfig,
    HandlerDiffFetcher,
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
from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFindingSeverity,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concrete DiffFetcher adapter — wraps async HandlerDiffFetcher to the sync protocol
# ---------------------------------------------------------------------------


class _DiffFetcherAdapter(ProtocolDiffFetcher):
    """Adapts the async HandlerDiffFetcher to the synchronous ProtocolDiffFetcher."""

    def __init__(self, handler: HandlerDiffFetcher) -> None:
        self._handler = handler

    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        # Run the async fetch in a dedicated thread so this sync wrapper is safe
        # to call from both sync and already-running-event-loop contexts.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self._handler.fetch(pr_number=pr_number, repo=repo),
            )
            return future.result()


# ---------------------------------------------------------------------------
# Stub implementations for handlers not yet available (parallel PRs)
# ---------------------------------------------------------------------------


class _StubThreadPoster(ProtocolThreadPoster):
    """Stub thread poster — no-ops until OMN-7969 (HandlerThreadPoster) lands."""

    def post(
        self,
        pr_number: int,
        repo: str,
        findings: tuple[ReviewFinding, ...],
        dry_run: bool,
    ) -> list[ThreadState]:
        logger.info(
            "StubThreadPoster: would post %d thread(s) for PR #%d in %s (dry_run=%s)",
            len(findings),
            pr_number,
            repo,
            dry_run,
        )
        return []


class _StubThreadWatcher(ProtocolThreadWatcher):
    """Stub thread watcher — returns threads unchanged until OMN-7970 lands."""

    def watch(
        self,
        pr_number: int,
        repo: str,
        thread_states: tuple[ThreadState, ...],
    ) -> list[ThreadState]:
        logger.info(
            "StubThreadWatcher: watching %d thread(s) for PR #%d in %s",
            len(thread_states),
            pr_number,
            repo,
        )
        return list(thread_states)


class _StubJudgeVerifier(ProtocolJudgeVerifier):
    """Stub judge verifier — returns threads unchanged until OMN-7971 lands."""

    def verify(
        self,
        correlation_id: UUID,
        findings: tuple[ReviewFinding, ...],
        thread_states: tuple[ThreadState, ...],
        judge_model: str,
    ) -> list[ThreadState]:
        logger.info(
            "StubJudgeVerifier: verifying %d thread(s) with model=%s (correlation_id=%s)",
            len(thread_states),
            judge_model,
            correlation_id,
        )
        return list(thread_states)


class _StubReportPoster(ProtocolReportPoster):
    """Stub report poster — logs verdict until OMN-7972 (HandlerReportPoster) lands."""

    def post_summary(
        self,
        pr_number: int,
        repo: str,
        verdict: ReviewVerdict,
        dry_run: bool,
    ) -> None:
        logger.info(
            "StubReportPoster: verdict=%s for PR #%d in %s "
            "(findings=%d, threads_pass=%d, threads_fail=%d, dry_run=%s)",
            verdict.verdict,
            pr_number,
            repo,
            verdict.total_findings,
            verdict.threads_verified_pass,
            verdict.threads_verified_fail,
            dry_run,
        )


# ---------------------------------------------------------------------------
# WorkflowRunner output model
# ---------------------------------------------------------------------------


class WorkflowRunnerResult:
    """Result returned by run_review()."""

    __slots__ = ("correlation_id", "events", "final_state", "verdict")

    def __init__(
        self,
        *,
        correlation_id: UUID,
        verdict: ReviewVerdict,
        events: list[ModelPhaseTransitionEvent],
        final_state: object,
    ) -> None:
        self.correlation_id = correlation_id
        self.verdict = verdict
        self.events = events
        self.final_state = final_state


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_review(
    pr_number: int,
    repo: str,
    github_token: str | None = None,
    *,
    reviewer_models: list[str] | None = None,
    judge_model: str = "deepseek-r1",
    severity_threshold: EnumFindingSeverity = EnumFindingSeverity.MAJOR,
    dry_run: bool = False,
    max_findings_per_pr: int = 20,
    correlation_id: UUID | None = None,
) -> WorkflowRunnerResult:
    """Run the full PR review bot pipeline.

    Callable from contract dispatch — reads LLM/GitHub config from environment.

    Args:
        pr_number: GitHub PR number.
        repo: Repository in ``owner/repo`` format.
        github_token: GitHub token. Defaults to ``GITHUB_TOKEN`` env var.
        reviewer_models: Reviewer model identifiers. Defaults to
            ``["qwen3-coder-30b", "qwen3-14b"]``.
        judge_model: Judge model identifier. Must not be a build-loop model.
        severity_threshold: Minimum severity to post a review thread.
        dry_run: If True, no GitHub comments are posted.
        max_findings_per_pr: Cap on review threads to prevent spam.
        correlation_id: Unique run ID; auto-generated if not provided.

    Returns:
        WorkflowRunnerResult with the final verdict, all FSM transition events,
        and the terminal FSM state.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    run_id = correlation_id or uuid4()

    request = ReviewRequest(
        correlation_id=run_id,
        pr_number=pr_number,
        repo=repo,
        reviewer_models=reviewer_models or ["qwen3-coder-30b", "qwen3-14b"],
        judge_model=judge_model,
        severity_threshold=severity_threshold,
        dry_run=dry_run,
        max_findings_per_pr=max_findings_per_pr,
        requested_at=datetime.now(tz=UTC),
    )

    # Wire concrete implementations.
    # AdapterGitHubBridge is instantiated here so sub-handlers can receive it
    # when their concrete implementations replace the stubs (OMN-7969 to OMN-7972).
    _github_bridge = AdapterGitHubBridge(
        token_env_var="GITHUB_TOKEN" if not token else _inject_token_env(token)
    )
    diff_fetcher_config = DiffFetcherConfig(github_token=token)
    diff_fetcher_handler = HandlerDiffFetcher(diff_fetcher_config)
    diff_fetcher = _DiffFetcherAdapter(diff_fetcher_handler)

    # Concrete reviewer — reads model selection from caller (contract inputs).
    # context_window per reviewer model comes from contract.yaml model_routing.reviewer.context_window (112K).
    _reviewer_context_windows = dict.fromkeys(request.reviewer_models, 112000)
    _reviewer_config = LlmReviewerConfig(
        reviewer_models=request.reviewer_models,
        model_context_windows=_reviewer_context_windows,
    )
    reviewer: ProtocolReviewer = HandlerLlmReviewer(config=_reviewer_config)
    # Stub implementations for handlers from parallel PRs (OMN-7969 to OMN-7972)
    thread_poster: ProtocolThreadPoster = _StubThreadPoster()
    thread_watcher: ProtocolThreadWatcher = _StubThreadWatcher()
    judge_verifier: ProtocolJudgeVerifier = _StubJudgeVerifier()
    report_poster: ProtocolReportPoster = _StubReportPoster()

    # Run the FSM pipeline
    fsm = HandlerPrReviewBot()
    final_state, events, verdict = fsm.run_full_pipeline(
        request=request,
        diff_fetcher=diff_fetcher,
        reviewer=reviewer,
        thread_poster=thread_poster,
        thread_watcher=thread_watcher,
        judge_verifier=judge_verifier,
        report_poster=report_poster,
    )

    # Stamp the judge model used (FSM make_verdict leaves it blank)
    verdict = verdict.model_copy(update={"judge_model_used": request.judge_model})

    logger.info(
        "PR review bot completed: pr=%d repo=%s verdict=%s phase=%s events=%d",
        pr_number,
        repo,
        verdict.verdict,
        final_state.current_phase,
        len(events),
    )

    return WorkflowRunnerResult(
        correlation_id=run_id,
        verdict=verdict,
        events=events,
        final_state=final_state,
    )


def _inject_token_env(token: str) -> str:
    """Set the token in the environment and return the env var name.

    Avoids passing the token as a constructor argument to AdapterGitHubBridge
    (which reads from env by design).
    """
    env_var = "GITHUB_TOKEN"
    os.environ[env_var] = token
    return env_var


__all__: list[str] = [
    "WorkflowRunnerResult",
    "run_review",
]
