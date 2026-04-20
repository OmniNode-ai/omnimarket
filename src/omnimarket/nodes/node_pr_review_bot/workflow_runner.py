# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WorkflowRunner for node_pr_review_bot.

Wires the FSM handler (HandlerPrReviewBot) with concrete sub-handler
implementations: HandlerThreadPoster, HandlerThreadWatcher,
HandlerJudgeVerifier, HandlerReportPoster (OMN-7969, OMN-7970, OMN-7971,
OMN-7972 — swapped in at OMN-9351).

Entry point: run_review(pr_number, repo, github_token)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)
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
from omnimarket.nodes.node_pr_review_bot.handlers.handler_judge_verifier import (
    HandlerJudgeVerifier,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_report_poster import (
    HandlerReportPoster,
    ProtocolGitHubBridge,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_poster import (
    HandlerThreadPoster,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_watcher import (
    HandlerThreadWatcher,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFindingSeverity,
    ReviewRequest,
    ReviewVerdict,
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
# ReportPoster bridge adapter — bridges async AdapterGitHubBridge.post_pr_comment
# to the sync ProtocolGitHubBridge consumed by HandlerReportPoster.
# ---------------------------------------------------------------------------


class _ReportPosterBridgeAdapter(ProtocolGitHubBridge):
    """Adapt async ``AdapterGitHubBridge`` to the sync surface expected by
    ``HandlerReportPoster``.

    HandlerReportPoster defines its own minimal ``ProtocolGitHubBridge`` with
    a synchronous ``post_pr_comment``; the real bridge is async. This shim
    runs the async call in a dedicated executor so the FSM's synchronous
    report phase doesn't need to know about the event loop.
    """

    def __init__(self, bridge: AdapterGitHubBridge) -> None:
        self._bridge = bridge

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self._bridge.post_pr_comment(repo, pr_number, body),
            )
            future.result()


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
        reviewer_models: Reviewer model identifiers. Required — must be
            provided by the caller using model keys registered in
            ``ModelInferenceBridgeConfig.model_configs``. Raises ``ValueError``
            if empty/None (no defaults: unknown keys previously produced a
            silent-clean verdict).
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

    resolved_reviewer_models = reviewer_models or []
    if not resolved_reviewer_models:
        raise ValueError(  # error-ok: CLI argument validation at caller boundary
            "reviewer_models must be provided — no default model keys are registered. "
            "Pass explicit model keys from ModelInferenceBridgeConfig.model_configs."
        )

    request = ReviewRequest(
        correlation_id=run_id,
        pr_number=pr_number,
        repo=repo,
        reviewer_models=resolved_reviewer_models,
        judge_model=judge_model,
        severity_threshold=severity_threshold,
        dry_run=dry_run,
        max_findings_per_pr=max_findings_per_pr,
        requested_at=datetime.now(tz=UTC),
    )

    # GitHub bridge is shared across thread poster / watcher / report poster so
    # rate-limit state and auth are consistent. AdapterGitHubBridge reads
    # GITHUB_TOKEN from env by design — route the explicit token through env
    # when provided.
    if token:
        _inject_token_env(token)
    github_bridge = AdapterGitHubBridge(token_env_var="GITHUB_TOKEN")

    diff_fetcher_config = DiffFetcherConfig(github_token=token)
    diff_fetcher_handler = HandlerDiffFetcher(diff_fetcher_config)
    diff_fetcher = _DiffFetcherAdapter(diff_fetcher_handler)

    # Reviewer wiring — populate inference_bridge_config from env so
    # caller-supplied reviewer_models keys resolve to real endpoints.
    # context_window per reviewer model comes from contract.yaml
    # model_routing.reviewer.context_window (112K).
    inference_bridge_config = load_inference_bridge_config_from_env()
    _reviewer_context_windows = dict.fromkeys(request.reviewer_models, 112_000)
    _reviewer_config = LlmReviewerConfig(
        reviewer_models=request.reviewer_models,
        model_context_windows=_reviewer_context_windows,
        inference_bridge_config=inference_bridge_config,
    )
    reviewer: ProtocolReviewer = HandlerLlmReviewer(config=_reviewer_config)

    # Concrete GitHub-side handlers (OMN-7969..OMN-7972, swapped in at OMN-9351).
    thread_poster: ProtocolThreadPoster = HandlerThreadPoster(
        bridge=github_bridge,
        max_findings_per_pr=request.max_findings_per_pr,
    )
    thread_watcher: ProtocolThreadWatcher = HandlerThreadWatcher(
        github_bridge=github_bridge,
    )
    judge_verifier: ProtocolJudgeVerifier = HandlerJudgeVerifier(
        judge_model_id=request.judge_model,
    )
    report_poster: ProtocolReportPoster = HandlerReportPoster(
        github_bridge=_ReportPosterBridgeAdapter(github_bridge),
    )

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
