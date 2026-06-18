# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_hostile_reviewer_orchestrator (OMN-13210 / B1).

ORCHESTRATOR node. Multi-model adversarial code review, rebuilt canonically from
the deleted node_hostile_reviewer ``workflow`` shell.

The orchestrator consumes ``ModelHostileReviewerStartCommand`` and coordinates
canonical nodes/handlers — there is NO in-process FSM ``advance()``, NO shelled
``gh``, and NO ``asyncio.run`` inside ``handle()``:

1. node_github_diff_effect (EFFECT)        — resolve the PR diff / file content
2. node_review_prompt_builder_compute       — build per-route adversarial prompts
3. AdapterInferenceBridge (A1-rehomed)      — canonical inference fan-out
4. node_review_response_parser_compute      — tolerant-parse responses to findings
5. node_finding_aggregator_compute          — weighted-union dedup + verdict

It emits ``ModelHostileReviewerCompletedEvent`` on
``onex.evt.omnimarket.hostile-reviewer-completed.v1`` via
``ModelHandlerOutput.for_orchestrator(events=...)`` — the bus is the transport.

The concrete inference adapter is resolved per-model from the contract
``model_routing`` policy + the contract-declared route-config env. It is
injectable via the constructor for deterministic tests (DI; no global state,
no ``set_adapter()`` mutation).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)
from omnimarket.models.model_review_finding import (
    EnumReviewVerdict,
    ModelReviewFinding,
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
from omnimarket.nodes.node_hostile_reviewer_orchestrator.model_config_loader import (
    build_model_configs,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_completed_event import (
    ModelHostileReviewerCompletedEvent,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_phase import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
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

_log = logging.getLogger(__name__)
_HANDLER_ID = "node_hostile_reviewer_orchestrator"

DEFAULT_MODEL_CONTEXT_WINDOW = 32_000
DEFAULT_TIMEOUT_SECONDS = 90.0
_PROMPT_TEMPLATE_ID = "adversarial_reviewer_pr"


def _finding_to_aggregator_dict(finding: ModelReviewFinding) -> dict[str, object]:
    """Project a ModelReviewFinding onto the aggregator's required dict shape.

    node_finding_aggregator_compute consumes a dict at its wire boundary with
    {rule_id, file_path, line_start, severity, normalized_message}.
    """
    evidence = finding.evidence
    file_path = evidence.file_path if evidence and evidence.file_path else "<unknown>"
    # The aggregator output requires line_start > 0; review findings frequently
    # carry no line range (plan/diff-level), so default to line 1. line_start is
    # not a dedup key (dedup is file_path + rule_id + Jaccard), so this is safe.
    line_start = 1
    if evidence and evidence.line_range:
        line_start = max(1, int(evidence.line_range.get("start", 1)))
    return {
        "rule_id": finding.category.value,
        "file_path": file_path,
        "line_start": line_start,
        "severity": finding.severity.value,
        "normalized_message": f"{finding.title}: {finding.description}",
    }


class HandlerHostileReviewerOrchestrator:
    """ORCHESTRATOR — multi-model adversarial code review over the bus.

    Args:
        inference_adapter: Optional concrete inference adapter. When omitted, a
            per-model adapter is resolved from the contract ``model_routing``
            policy + route-config env at ``handle()`` time. Injecting an adapter
            keeps tests deterministic (no network, no env, no global mutation).
        github_diff_effect: Optional EFFECT used to resolve the review target.
            Injectable for deterministic tests.
    """

    def __init__(
        self,
        inference_adapter: ModelInferenceAdapter | None = None,
        github_diff_effect: HandlerGithubDiffEffect | None = None,
    ) -> None:
        self._inference_adapter = inference_adapter
        self._github_diff_effect = github_diff_effect or HandlerGithubDiffEffect()
        self._prompt_builder = HandlerPromptBuilderCompute()
        self._response_parser = HandlerResponseParserCompute()
        self._aggregator = HandlerFindingAggregator()

    async def handle(
        self, command: ModelHostileReviewerStartCommand
    ) -> ModelHandlerOutput[None]:
        """Run the adversarial review and emit the completed event.

        ORCHESTRATOR output: events only (the completed event). On any failure
        the orchestrator still emits a completed event with final_phase=FAILED
        and an error_message — silence on failure is worse than a typed failure.
        """
        started_at = datetime.now(tz=UTC)
        try:
            completed = await self._run_review(command, started_at)
        except Exception as exc:  # boundary-ok: orchestrator emits typed failure event
            _log.error(
                "hostile reviewer orchestration failed (correlation_id=%s): %s",
                command.correlation_id,
                exc,
            )
            completed = ModelHostileReviewerCompletedEvent(
                correlation_id=command.correlation_id,
                final_phase=EnumHostileReviewerPhase.FAILED,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                pass_count=0,
                total_findings=0,
                error_message=str(exc),
            )

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(completed,),
        )

    async def _run_review(
        self,
        command: ModelHostileReviewerStartCommand,
        started_at: datetime,
    ) -> ModelHostileReviewerCompletedEvent:
        diff_content = await self._resolve_diff(command)
        adapter = self._resolve_adapter(command.models)

        per_model_findings = await self._fan_out_reviews(
            command=command,
            diff_content=diff_content,
            adapter=adapter,
        )

        total_findings = await self._aggregate(
            command.correlation_id, per_model_findings
        )

        return ModelHostileReviewerCompletedEvent(
            correlation_id=command.correlation_id,
            final_phase=EnumHostileReviewerPhase.DONE,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            pass_count=len(command.models),
            total_findings=total_findings,
            error_message=None,
        )

    async def _resolve_diff(self, command: ModelHostileReviewerStartCommand) -> str:
        """Dispatch node_github_diff_effect to resolve the review target."""
        diff_command = ModelGithubDiffCommand(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
            file_path=command.file_path,
        )
        output = await self._github_diff_effect.handle(diff_command)
        resolved = output.events[0]
        if not isinstance(resolved, ModelGithubDiffResolvedEvent):
            raise TypeError(
                "node_github_diff_effect returned an unexpected event type: "
                f"{type(resolved).__name__}"
            )
        return resolved.content

    def _resolve_adapter(self, models: list[str]) -> ModelInferenceAdapter:
        if self._inference_adapter is not None:
            return self._inference_adapter
        configs = build_model_configs(requested_keys=models)
        return AdapterInferenceBridge(ModelInferenceBridgeConfig(model_configs=configs))

    async def _fan_out_reviews(
        self,
        command: ModelHostileReviewerStartCommand,
        diff_content: str,
        adapter: ModelInferenceAdapter,
    ) -> dict[str, list[ModelReviewFinding]]:
        """Fan out per-model: build prompt -> infer -> parse. Parallel."""
        tasks = [
            self._review_one_model(
                command=command,
                model_key=model_key,
                diff_content=diff_content,
                adapter=adapter,
            )
            for model_key in command.models
        ]
        results = await asyncio.gather(*tasks)
        per_model: dict[str, list[ModelReviewFinding]] = {}
        for model_key, findings in results:
            if findings:
                per_model[model_key] = findings
        return per_model

    async def _review_one_model(
        self,
        command: ModelHostileReviewerStartCommand,
        model_key: str,
        diff_content: str,
        adapter: ModelInferenceAdapter,
    ) -> tuple[str, list[ModelReviewFinding]]:
        prompt = await self._build_prompt(command.correlation_id, diff_content)
        try:
            raw = await adapter.infer(
                model_key=model_key,
                system_prompt=prompt.system_prompt,
                user_prompt=prompt.user_prompt,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # boundary-ok: per-model degradation, not fatal
            _log.warning("review model %s failed: %s", model_key, exc)
            return model_key, []

        parsed = await self._parse_response(command.correlation_id, model_key, raw)
        return model_key, list(parsed.findings)

    async def _build_prompt(
        self, correlation_id: UUID, diff_content: str
    ) -> ModelPromptBuilderOutput:
        output = await self._prompt_builder.handle(
            ModelReviewPromptBuilderRequest(
                correlation_id=correlation_id,
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
        self, correlation_id: UUID, model_key: str, raw_text: str
    ) -> ModelParseResult:
        output = await self._response_parser.handle(
            ModelReviewResponseParserRequest(
                correlation_id=correlation_id,
                raw_text=raw_text,
                source_model=model_key,
            )
        )
        result = output.result
        if not isinstance(result, ModelParseResult):
            raise TypeError("response parser returned an unexpected result type")
        return result

    async def _aggregate(
        self,
        correlation_id: UUID,
        per_model_findings: dict[str, list[ModelReviewFinding]],
    ) -> int:
        """Dispatch node_finding_aggregator_compute for dedup + verdict.

        Returns the total pre-dedup finding count carried by the completed
        event. The aggregator's verdict is logged for observability; the
        preserved ModelHostileReviewerCompletedEvent shape carries no verdict
        field, so it is not surfaced on the event.
        """
        if not per_model_findings:
            return 0

        sources = tuple(
            ModelSourceFindings(
                model_name=model_key,
                findings=tuple(_finding_to_aggregator_dict(f) for f in findings),
            )
            for model_key, findings in per_model_findings.items()
        )
        output = await self._aggregator.handle(
            correlation_id=correlation_id,
            input_data=ModelFindingAggregatorInput(
                correlation_id=correlation_id,
                sources=sources,
            ),
        )
        verdict = _verdict_from_aggregated(output.verdict.value)
        _log.info(
            "hostile reviewer verdict=%s (correlation_id=%s, merged=%d)",
            verdict.value,
            correlation_id,
            output.total_merged_findings,
        )
        return sum(len(f) for f in per_model_findings.values())


def _verdict_from_aggregated(aggregated_value: str) -> EnumReviewVerdict:
    """Map the aggregator's verdict vocabulary onto the review verdict enum."""
    mapping = {
        "clean": EnumReviewVerdict.CLEAN,
        "risks_noted": EnumReviewVerdict.RISKS_NOTED,
        "blocking_issue": EnumReviewVerdict.BLOCKING_ISSUE,
    }
    return mapping.get(aggregated_value, EnumReviewVerdict.RISKS_NOTED)


__all__: list[str] = ["HandlerHostileReviewerOrchestrator"]
