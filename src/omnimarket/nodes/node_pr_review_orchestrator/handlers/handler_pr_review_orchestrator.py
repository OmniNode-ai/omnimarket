# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_pr_review_orchestrator (OMN-13212 / B2).

ORCHESTRATOR node. Automated multi-model PR review with a judge-verification
gate, rebuilt canonically from the deleted node_pr_review_bot ``workflow`` shell.

The orchestrator consumes ``ReviewRequest`` and coordinates canonical
nodes/handlers — there is NO in-process FSM ``run_full_pipeline`` driving
sibling handlers via bespoke protocols, NO shelled ``gh``, NO raw-httpx judge
call, and NO ``asyncio.run`` / ``ThreadPoolExecutor`` inside ``handle()``:

  FSM phase                canonical node / handler
  ------------------------ --------------------------------------------------
  FETCH_DIFF               node_github_diff_effect (EFFECT)
  REVIEW                   node_review_prompt_builder_compute + inference bridge
                           (A1) + node_review_response_parser_compute
  (dedup)                  node_finding_aggregator_compute (COMPUTE)
  POST_THREADS             node_github_review_effect (EFFECT, post_threads)
  WATCH                    node_github_review_effect (EFFECT, watch_threads)
  JUDGE_VERIFY             inference bridge (judge call) +
                           node_judge_verdict_parse_compute (COMPUTE)
  REPORT                   node_github_review_effect (EFFECT, post_report)

Phase progression + the 3-failure circuit breaker are folded by the pure FSM
helpers in ``omnimarket.review.pr_review_fsm`` (the same fold the
node_pr_review_fsm_reducer REDUCER applies). The orchestrator emits
``ModelPrReviewCompletedEvent`` (preserving the ``ReviewVerdict`` shape) on
``onex.evt.omnimarket.pr-review-bot-completed.v1`` via
``ModelHandlerOutput.for_orchestrator(events=...)`` — the bus is the transport.

The inference adapter is resolved per-run from the contract ``model_routing``
policy + route-config env, and is injectable for deterministic tests (DI; no
global state, no ``set_adapter()`` mutation).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_finding_aggregator_compute.handlers.handler_finding_aggregator import (
    HandlerFindingAggregator,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_input import (
    ModelFindingAggregatorInput,
    ModelSourceFindings,
)
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_github_review_effect.handlers.handler_github_review_effect import (
    HandlerGithubReviewEffect,
)
from omnimarket.nodes.node_judge_verdict_parse_compute.handlers.handler_judge_verdict_parse_compute import (
    HandlerJudgeVerdictParseCompute,
)
from omnimarket.nodes.node_pr_review_orchestrator.model_config_loader import (
    build_bridge_config,
)
from omnimarket.nodes.node_pr_review_orchestrator.models.model_pr_review_completed_event import (
    ModelPrReviewCompletedEvent,
)
from omnimarket.nodes.node_review_prompt_builder_compute.handlers.handler_prompt_builder_compute import (
    HandlerPromptBuilderCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)
from omnimarket.review.node_io import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
    ModelParseResult,
    ModelPromptBuilderOutput,
    ModelReviewPromptBuilderRequest,
    ModelReviewResponseParserRequest,
)
from omnimarket.review.pr_review_fsm import (
    ModelPrReviewBotState,
    advance,
    make_verdict,
    start_state,
)
from omnimarket.review.pr_review_io import (
    EnumFsmPhase,
    EnumThreadStatus,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)
from omnimarket.review.pr_review_node_io import (
    EnumGithubReviewOperation,
    ModelGithubReviewCommand,
    ModelJudgeParseRequest,
)

_log = logging.getLogger(__name__)
_HANDLER_ID = "node_pr_review_orchestrator"

DEFAULT_MODEL_CONTEXT_WINDOW = 32_000
DEFAULT_TIMEOUT_SECONDS = 90.0
JUDGE_TIMEOUT_SECONDS = 90.0
MAX_VERIFY_ATTEMPTS = 3
_PROMPT_TEMPLATE_ID = "adversarial_reviewer_pr"

_JUDGE_SYSTEM_PROMPT = """\
You are a senior code review judge. Determine whether an author's response \
adequately addresses a code review finding.

Respond ONLY with valid JSON in this exact format:
{"verdict": "PASS" | "FAIL", "reasoning": "<one or two sentences>"}

PASS when the reply explains a concrete fix that addresses the finding and the \
diff confirms it (or the finding was a false positive convincingly explained).
FAIL when the reply is vague/dismissive, the code is unchanged, or the \
explanation is technically incorrect. Do not add commentary outside the JSON.
"""


def _finding_to_aggregator_dict(finding: ReviewFinding) -> dict[str, object]:
    """Project a ReviewFinding onto the aggregator's required dict shape."""
    line_start = finding.evidence.line_start or 1
    return {
        "rule_id": finding.category.value,
        "file_path": finding.evidence.file_path or "<unknown>",
        "line_start": max(1, line_start),
        "severity": finding.severity.value,
        "normalized_message": f"{finding.title}: {finding.description}",
    }


def _build_judge_prompt(finding: ReviewFinding, conversation: str | None) -> str:
    """Build the judge user prompt from a finding + thread conversation."""
    finding_block = (
        f"## Finding\nTitle: {finding.title}\nSeverity: {finding.severity}\n"
        f"Category: {finding.category}\nDescription: {finding.description}\n"
    )
    if finding.suggestion:
        finding_block += f"Suggested fix: {finding.suggestion}\n"
    if finding.evidence.file_path:
        line_info = ""
        if finding.evidence.line_start is not None:
            end = finding.evidence.line_end or finding.evidence.line_start
            line_info = f", lines {finding.evidence.line_start}-{end}"
        finding_block += f"File: {finding.evidence.file_path}{line_info}\n"
    convo_block = "## Author Thread Replies\n"
    convo_block += (
        conversation
        if conversation
        else "(No author replies — thread was silently dismissed.)\n"
    )
    return f"{finding_block}\n{convo_block}"


class HandlerPrReviewOrchestrator:
    """ORCHESTRATOR — automated PR review with judge-verification gate over the bus.

    Args:
        inference_adapter: Optional concrete inference adapter for reviewer + judge
            fan-out. When omitted, an adapter is resolved per-run from the
            contract ``model_routing`` policy + route-config env. Injecting an
            adapter keeps tests deterministic (no network, no env, no global
            mutation).
        github_diff_effect: Optional EFFECT used to resolve the PR diff.
        github_review_effect: Optional EFFECT used for thread post/watch/report.
    """

    def __init__(
        self,
        inference_adapter: ModelInferenceAdapter | None = None,
        github_diff_effect: HandlerGithubDiffEffect | None = None,
        github_review_effect: HandlerGithubReviewEffect | None = None,
    ) -> None:
        self._inference_adapter = inference_adapter
        self._github_diff_effect = github_diff_effect or HandlerGithubDiffEffect()
        self._github_review_effect = github_review_effect or HandlerGithubReviewEffect()
        self._prompt_builder = HandlerPromptBuilderCompute()
        self._response_parser = HandlerResponseParserCompute()
        self._aggregator = HandlerFindingAggregator()
        self._judge_parser = HandlerJudgeVerdictParseCompute()

    async def handle(self, command: ReviewRequest) -> ModelHandlerOutput[None]:
        """Run the PR review pipeline and emit the completed event.

        ORCHESTRATOR output: events only. On any failure the orchestrator still
        emits a completed event with a fail-closed BLOCKING_ISSUE verdict and the
        terminal FSM phase — silence on failure is worse than a typed failure.
        """
        state = start_state(command)
        try:
            state = await self._run_pipeline(command, state)
        except Exception as exc:  # boundary-ok: orchestrator emits typed failure event
            _log.error(
                "pr review orchestration failed (correlation_id=%s): %s",
                command.correlation_id,
                exc,
            )
            # Force the FSM into FAILED so make_verdict fails closed.
            state = self._force_failed(state, str(exc))

        verdict = make_verdict(state, judge_model_used=command.judge_model)
        completed = ModelPrReviewCompletedEvent(
            final_phase=state.current_phase, verdict=verdict
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(completed,),
        )

    async def _run_pipeline(
        self, command: ReviewRequest, state: ModelPrReviewBotState
    ) -> ModelPrReviewBotState:
        # INIT -> FETCH_DIFF
        state, _ = advance(state, phase_success=True)

        # FETCH_DIFF -> REVIEW
        diff_content = await self._resolve_diff(command)
        state, _ = advance(state, phase_success=True)

        # REVIEW -> POST_THREADS
        findings = await self._fan_out_review(command, diff_content)
        await self._aggregate(command, findings)
        state, _ = advance(state, phase_success=True, findings=tuple(findings))

        # POST_THREADS -> WATCH
        thread_states = await self._post_threads(command, findings)
        state, _ = advance(
            state, phase_success=True, thread_states=tuple(thread_states)
        )

        # WATCH -> JUDGE_VERIFY
        thread_states = await self._watch_threads(command, thread_states)
        state, _ = advance(
            state, phase_success=True, thread_states=tuple(thread_states)
        )

        # JUDGE_VERIFY -> REPORT
        thread_states = await self._judge_verify(command, findings, thread_states)
        state, _ = advance(
            state, phase_success=True, thread_states=tuple(thread_states)
        )

        # REPORT -> DONE
        report_verdict = make_verdict(state, judge_model_used=command.judge_model)
        await self._post_report(command, findings, thread_states, report_verdict)
        state, _ = advance(state, phase_success=True)

        return state

    @staticmethod
    def _force_failed(
        state: ModelPrReviewBotState, error_message: str
    ) -> ModelPrReviewBotState:
        """Drive the FSM to FAILED via repeated failed advances (circuit breaker)."""
        if state.current_phase in {EnumFsmPhase.DONE, EnumFsmPhase.FAILED}:
            return state
        while state.current_phase != EnumFsmPhase.FAILED:
            state, _ = advance(state, phase_success=False, error_message=error_message)
        return state

    # ------------------------------------------------------------------
    # FETCH_DIFF
    # ------------------------------------------------------------------

    async def _resolve_diff(self, command: ReviewRequest) -> str:
        diff_command = ModelGithubDiffCommand(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
        )
        output = await self._github_diff_effect.handle(diff_command)
        resolved = output.events[0]
        if not isinstance(resolved, ModelGithubDiffResolvedEvent):
            raise TypeError(
                "node_github_diff_effect returned an unexpected event type: "
                f"{type(resolved).__name__}"
            )
        return resolved.content

    # ------------------------------------------------------------------
    # REVIEW
    # ------------------------------------------------------------------

    def _resolve_adapter(self, command: ReviewRequest) -> ModelInferenceAdapter:
        if self._inference_adapter is not None:
            return self._inference_adapter
        config = build_bridge_config(command.reviewer_models, command.judge_model)
        return AdapterInferenceBridge(config)

    async def _fan_out_review(
        self, command: ReviewRequest, diff_content: str
    ) -> list[ReviewFinding]:
        adapter = self._resolve_adapter(command)
        prompt = await self._build_prompt(command, diff_content)
        findings: list[ReviewFinding] = []
        for model_key in command.reviewer_models:
            try:
                raw = await adapter.infer(
                    model_key=model_key,
                    system_prompt=prompt.system_prompt,
                    user_prompt=prompt.user_prompt,
                    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # boundary-ok: per-model degradation, not fatal
                _log.warning("review model %s failed: %s", model_key, exc)
                continue
            parsed = await self._parse_response(command, model_key, raw)
            findings.extend(
                ReviewFinding.from_model_review_finding(f) for f in parsed.findings
            )
        return findings

    async def _build_prompt(
        self, command: ReviewRequest, diff_content: str
    ) -> ModelPromptBuilderOutput:
        output = await self._prompt_builder.handle(
            ModelReviewPromptBuilderRequest(
                correlation_id=command.correlation_id,
                prompt_template_id=_PROMPT_TEMPLATE_ID,
                context_content=diff_content,
                model_context_window=DEFAULT_MODEL_CONTEXT_WINDOW,
            )
        )
        result = output.result
        if not isinstance(result, ModelPromptBuilderOutput):
            raise TypeError("prompt builder returned an unexpected result type")
        return result

    async def _parse_response(
        self, command: ReviewRequest, model_key: str, raw_text: str
    ) -> ModelParseResult:
        output = await self._response_parser.handle(
            ModelReviewResponseParserRequest(
                correlation_id=command.correlation_id,
                raw_text=raw_text,
                source_model=model_key,
            )
        )
        result = output.result
        if not isinstance(result, ModelParseResult):
            raise TypeError("response parser returned an unexpected result type")
        return result

    async def _aggregate(
        self, command: ReviewRequest, findings: list[ReviewFinding]
    ) -> None:
        """Dispatch node_finding_aggregator_compute for dedup + verdict (observed)."""
        if not findings:
            return
        by_model: dict[str, list[ReviewFinding]] = {}
        for f in findings:
            by_model.setdefault(f.source_model, []).append(f)
        sources = tuple(
            ModelSourceFindings(
                model_name=model_key,
                findings=tuple(_finding_to_aggregator_dict(f) for f in model_findings),
            )
            for model_key, model_findings in by_model.items()
        )
        output = await self._aggregator.handle(
            correlation_id=command.correlation_id,
            input_data=ModelFindingAggregatorInput(
                correlation_id=command.correlation_id, sources=sources
            ),
        )
        _log.info(
            "pr review aggregator verdict=%s (correlation_id=%s, merged=%d)",
            output.verdict.value,
            command.correlation_id,
            output.total_merged_findings,
        )

    # ------------------------------------------------------------------
    # POST_THREADS / WATCH / REPORT (github review EFFECT)
    # ------------------------------------------------------------------

    async def _post_threads(
        self, command: ReviewRequest, findings: list[ReviewFinding]
    ) -> list[ThreadState]:
        output = await self._github_review_effect.handle(
            ModelGithubReviewCommand(
                correlation_id=command.correlation_id,
                operation=EnumGithubReviewOperation.POST_THREADS,
                repo=command.repo,
                pr_number=command.pr_number,
                dry_run=command.dry_run,
                max_findings_per_pr=command.max_findings_per_pr,
                findings=tuple(findings),
            )
        )
        return list(output.events[0].thread_states)

    async def _watch_threads(
        self, command: ReviewRequest, thread_states: list[ThreadState]
    ) -> list[ThreadState]:
        if not thread_states:
            return thread_states
        output = await self._github_review_effect.handle(
            ModelGithubReviewCommand(
                correlation_id=command.correlation_id,
                operation=EnumGithubReviewOperation.WATCH_THREADS,
                repo=command.repo,
                pr_number=command.pr_number,
                dry_run=command.dry_run,
                thread_states=tuple(thread_states),
            )
        )
        return list(output.events[0].thread_states)

    async def _post_report(
        self,
        command: ReviewRequest,
        findings: list[ReviewFinding],
        thread_states: list[ThreadState],
        verdict: ReviewVerdict,
    ) -> None:
        await self._github_review_effect.handle(
            ModelGithubReviewCommand(
                correlation_id=command.correlation_id,
                operation=EnumGithubReviewOperation.POST_REPORT,
                repo=command.repo,
                pr_number=command.pr_number,
                dry_run=command.dry_run,
                findings=tuple(findings),
                thread_states=tuple(thread_states),
                verdict=verdict,
            )
        )

    # ------------------------------------------------------------------
    # JUDGE_VERIFY (inference judge call + judge parse COMPUTE)
    # ------------------------------------------------------------------

    async def _judge_verify(
        self,
        command: ReviewRequest,
        findings: list[ReviewFinding],
        thread_states: list[ThreadState],
    ) -> list[ThreadState]:
        resolved = [t for t in thread_states if t.status == EnumThreadStatus.RESOLVED]
        if not resolved:
            return thread_states

        findings_by_id = {f.id: f for f in findings}

        for thread in thread_states:
            if thread.status != EnumThreadStatus.RESOLVED:
                continue
            if thread.verify_attempts >= MAX_VERIFY_ATTEMPTS:
                thread.status = EnumThreadStatus.ESCALATED
                thread.judge_reasoning = (
                    f"Escalated after {MAX_VERIFY_ATTEMPTS} verification attempts. "
                    "Human review required."
                )
                continue
            finding = findings_by_id.get(thread.finding_id)
            if finding is None:
                continue
            passed, reasoning = await self._call_judge(command, finding, thread)
            thread.verify_attempts += 1
            thread.judge_reasoning = reasoning
            thread.status = (
                EnumThreadStatus.VERIFIED_PASS
                if passed
                else EnumThreadStatus.VERIFIED_FAIL
            )
        return thread_states

    async def _call_judge(
        self,
        command: ReviewRequest,
        finding: ReviewFinding,
        thread: ThreadState,
    ) -> tuple[bool, str]:
        adapter = self._resolve_adapter(command)
        user_prompt = _build_judge_prompt(finding, thread.judge_reasoning)
        try:
            raw = await adapter.infer(
                model_key=command.judge_model,
                system_prompt=_JUDGE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                timeout_seconds=JUDGE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # boundary-ok: judge failure -> fail-closed FAIL
            _log.warning("judge call failed for finding %s: %s", finding.id, exc)
            return False, f"Judge model call failed: {exc}. Treating as FAIL."

        output = await self._judge_parser.handle(
            ModelJudgeParseRequest(correlation_id=command.correlation_id, raw_text=raw)
        )
        result = output.result
        if result is None:
            return False, "Judge parse returned no result. Treating as FAIL."
        return result.passed, result.reasoning


__all__: list[str] = ["HandlerPrReviewOrchestrator"]
