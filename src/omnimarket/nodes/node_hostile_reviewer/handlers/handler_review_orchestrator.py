# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Review Orchestrator — wires FSM to inference, prompt builder, parser, aggregator.

The orchestrator fans out prompts to N models in parallel, collects responses,
parses them, and aggregates findings to produce a verdict. It does NOT own prompt
construction, response parsing, convergence decisions, or model selection policy.

This is the only layer that coordinates I/O (via the injected inference adapter).

Reference: OMN-7797, OMN-7781
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    build_prompt,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    EnumParseStatus,
    parse_model_response,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingSeverity,
    EnumReviewVerdict,
    ModelReviewFinding,
)

logger = logging.getLogger(__name__)


class ModelInferenceAdapter(ABC):
    """Protocol for dispatching inference to node_llm_inference_effect."""

    @abstractmethod
    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        """Send prompt to a model and return raw response text."""
        ...


class ModelMergedFinding(BaseModel):
    """A finding that may have been reported by multiple models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(...)
    description: str = Field(...)
    severity: EnumFindingSeverity = Field(...)
    source_models: tuple[str, ...] = Field(default_factory=tuple)
    location: str | None = Field(default=None)


class ModelPerModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(...)
    raw_response_length: int = Field(default=0)
    findings_count: int = Field(default=0)
    parse_status: str = Field(default="")
    error_message: str = Field(default="")
    latency_ms: float = Field(default=0.0)


class ModelOrchestratorInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    diff_content: str = Field(...)
    model_keys: list[str] = Field(...)
    model_context_windows: dict[str, int] = Field(...)
    prompt_template_id: str = Field(default="adversarial_reviewer_pr")
    persona_markdown: str | None = Field(default=None)
    default_timeout_seconds: float = Field(default=90.0)


class ModelOrchestratorOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    merged_findings: list[ModelMergedFinding] = Field(default_factory=list)
    verdict: EnumReviewVerdict = Field(...)
    per_model_results: list[ModelPerModelResult] = Field(default_factory=list)
    models_succeeded: tuple[str, ...] = Field(default_factory=tuple)
    models_failed: tuple[str, ...] = Field(default_factory=tuple)
    total_input_findings: int = Field(default=0)


def _aggregate_findings(
    per_model_findings: dict[str, list[ModelReviewFinding]],
) -> tuple[list[ModelMergedFinding], EnumReviewVerdict, int]:
    """Aggregate findings from multiple models into merged findings with verdict.

    Uses title-based dedup: findings with identical titles from different models
    are merged, with severity promoted to the highest reported.
    """
    title_clusters: dict[str, ModelMergedFinding] = {}
    total_input = 0

    severity_order = {
        EnumFindingSeverity.NIT: 0,
        EnumFindingSeverity.MINOR: 1,
        EnumFindingSeverity.MAJOR: 2,
        EnumFindingSeverity.CRITICAL: 3,
    }

    for model_key, findings in per_model_findings.items():
        for finding in findings:
            total_input += 1
            key = finding.title.lower().strip()
            existing = title_clusters.get(key)
            if existing is not None:
                # Merge: promote severity, add source model
                new_severity = (
                    finding.severity
                    if severity_order.get(finding.severity, 0)
                    > severity_order.get(existing.severity, 0)
                    else existing.severity
                )
                title_clusters[key] = ModelMergedFinding(
                    title=existing.title,
                    description=existing.description,
                    severity=new_severity,
                    source_models=(*existing.source_models, model_key),
                    location=existing.location,
                )
            else:
                location = finding.evidence.file_path if finding.evidence else None
                title_clusters[key] = ModelMergedFinding(
                    title=finding.title,
                    description=finding.description,
                    severity=finding.severity,
                    source_models=(model_key,),
                    location=location,
                )

    merged = list(title_clusters.values())

    # Determine verdict
    verdict = EnumReviewVerdict.CLEAN
    for f in merged:
        if f.severity in (EnumFindingSeverity.CRITICAL, EnumFindingSeverity.MAJOR):
            verdict = EnumReviewVerdict.BLOCKING_ISSUE
            break
        if f.severity in (EnumFindingSeverity.MINOR, EnumFindingSeverity.NIT):
            verdict = EnumReviewVerdict.RISKS_NOTED

    return merged, verdict, total_input


async def _dispatch_single_model(
    model_key: str,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
    adapter: ModelInferenceAdapter,
) -> tuple[str, str | None, str | None]:
    """Dispatch to a single model. Returns (model_key, raw_response, error)."""
    try:
        raw = await adapter.infer(
            model_key, system_prompt, user_prompt, timeout_seconds
        )
        return model_key, raw, None
    except Exception as e:
        logger.warning("Model %s failed: %s", model_key, e)
        return model_key, None, str(e)


async def run_review_orchestration(
    input_data: ModelOrchestratorInput,
    inference_adapter: ModelInferenceAdapter,
) -> ModelOrchestratorOutput:
    """Fan-out to N models, parse responses, aggregate findings."""
    per_model_findings: dict[str, list[ModelReviewFinding]] = {}
    per_model_results: list[ModelPerModelResult] = []
    succeeded: list[str] = []
    failed: list[str] = []

    # Build per-model prompts (context window may differ)
    tasks = []
    for model_key in input_data.model_keys:
        ctx_window = input_data.model_context_windows.get(model_key, 32_000)
        prompt_output = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id=input_data.prompt_template_id,
                context_content=input_data.diff_content,
                model_context_window=ctx_window,
                persona_markdown=input_data.persona_markdown,
            )
        )
        tasks.append(
            _dispatch_single_model(
                model_key=model_key,
                system_prompt=prompt_output.system_prompt,
                user_prompt=prompt_output.user_prompt,
                timeout_seconds=input_data.default_timeout_seconds,
                adapter=inference_adapter,
            )
        )

    # Fan-out in parallel
    results = await asyncio.gather(*tasks)

    for model_key, raw_response, error in results:
        if error is not None or raw_response is None:
            failed.append(model_key)
            per_model_results.append(
                ModelPerModelResult(
                    model_key=model_key,
                    error_message=error or "No response",
                    parse_status="transport_failure",
                )
            )
            continue

        parse_result = parse_model_response(raw_response, source_model=model_key)
        per_model_results.append(
            ModelPerModelResult(
                model_key=model_key,
                raw_response_length=len(raw_response),
                findings_count=len(parse_result.findings),
                parse_status=parse_result.status,
                error_message=parse_result.error_message,
            )
        )

        if parse_result.status == EnumParseStatus.SUCCESS:
            succeeded.append(model_key)
            if parse_result.findings:
                per_model_findings[model_key] = parse_result.findings
        else:
            failed.append(model_key)

    # Aggregate
    merged_findings, verdict, total_input = _aggregate_findings(per_model_findings)

    return ModelOrchestratorOutput(
        correlation_id=input_data.correlation_id,
        merged_findings=merged_findings,
        verdict=verdict,
        per_model_results=per_model_results,
        models_succeeded=tuple(succeeded),
        models_failed=tuple(failed),
        total_input_findings=total_input,
    )


__all__: list[str] = [
    "ModelInferenceAdapter",
    "ModelMergedFinding",
    "ModelOrchestratorInput",
    "ModelOrchestratorOutput",
    "ModelPerModelResult",
    "run_review_orchestration",
]
