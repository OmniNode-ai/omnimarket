# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerPrSemanticGrader -- LLM-based semantic grader for PR acceptance criteria.

Uses a configurable LLM to score whether a PR diff satisfies a ticket's acceptance
criteria across four dimensions: criteria_coverage, contract_alignment,
anti_pattern_present, and overall_confidence.

Phase 1: advisory-only. anti_pattern_present >= 0.7 sets advisory=True but does
not block merge. Phase 2 adds hard-fail after calibration.

On grader failure, returns success=False with error details rather than zero scores
(zero scores are indistinguishable from a genuinely poor implementation).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_request import (
    ModelSemanticGradingRequest,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_result import (
    ModelLLMCallEvidence,
    ModelSemanticGradingResult,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert code reviewer evaluating whether a pull request diff "
    "semantically satisfies a ticket's acceptance criteria.\n"
    "\n"
    "IMPORTANT: This is advisory evaluation for Phase 1 calibration. Your scores "
    "help detect semantic gaps — they are not authoritative merge decisions.\n"
    "\n"
    "Evaluate the PR diff against the acceptance criteria on exactly four dimensions:\n"
    "\n"
    "- criteria_coverage: fraction of acceptance criteria explicitly addressed by the diff "
    "(0.0 = none addressed, 1.0 = all addressed)\n"
    "- contract_alignment: degree to which the implementation uses contract-driven lookups "
    "rather than hardcoded strings, topics, event types, or keys "
    "(0.0 = all hardcoded, 1.0 = fully contract-driven)\n"
    "- anti_pattern_present: presence of explicitly forbidden patterns such as hardcoded "
    "topic strings, event_type literals, direct coupling, or bypassed contract resolution "
    "(0.0 = no violations, 1.0 = severe violations — NOTE: this dimension is INVERTED)\n"
    "- overall_confidence: your confidence in this assessment given the available context "
    "(0.0 = very uncertain, 1.0 = high confidence)\n"
    "\n"
    "Respond with ONLY a JSON object in this exact format, no prose:\n"
    '{"criteria_coverage": <float>, "contract_alignment": <float>, '
    '"anti_pattern_present": <float>, "overall_confidence": <float>, '
    '"rationale": "<one sentence per dimension separated by |>"}\n'
)


def _build_user_prompt(request: ModelSemanticGradingRequest) -> str:
    criteria_block = "\n".join(f"- {c}" for c in request.acceptance_criteria)
    return (
        f"## Ticket: {request.ticket_id}\n\n"
        f"## Acceptance Criteria\n\n{criteria_block}\n\n"
        f"## PR Title\n\n{request.pr_title}\n\n"
        f"## PR Diff\n\n```diff\n{request.pr_diff_text}\n```\n\n"
        "Score the diff against the acceptance criteria on the four dimensions."
    )


def _parse_scores(raw: str) -> tuple[dict[str, float], str | None] | None:
    """Extract JSON scores from LLM response. Returns (scores, rationale) or None on failure."""
    match = re.search(r"\{[^}]+\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    required = {
        "criteria_coverage",
        "contract_alignment",
        "anti_pattern_present",
        "overall_confidence",
    }
    if not required.issubset(data.keys()):
        return None

    scores: dict[str, float] = {}
    for key in required:
        raw_val = data[key]
        if isinstance(raw_val, bool):
            return None
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            return None
        if not (0.0 <= val <= 1.0):
            return None
        scores[key] = val

    rationale: str | None = None
    if "rationale" in data and isinstance(data["rationale"], str):
        rationale = data["rationale"]

    return scores, rationale


class HandlerPrSemanticGrader:
    """LLM-based grader for PR semantic satisfaction of acceptance criteria.

    Inject ``inference_bridge`` in tests to avoid real network calls.
    Config is read from the contract; only API keys remain in env.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        grader_model_key: str = "opus",
        grader_timeout_seconds: float = 120.0,
        prompt_template_id: str = "pr_semantic_grading_v1",
        prompt_template_version: str = "1.0.0",
    ) -> None:
        self._bridge: ModelInferenceAdapter = (
            inference_bridge or AdapterInferenceBridge(ModelInferenceBridgeConfig())
        )
        self._grader_model_key = grader_model_key
        self._grader_timeout_seconds = grader_timeout_seconds
        self._prompt_template_id = prompt_template_id
        self._prompt_template_version = prompt_template_version

    async def handle(
        self, request: ModelSemanticGradingRequest
    ) -> ModelSemanticGradingResult:
        logger.info(
            "pr-semantic-grading started (ticket_id=%s, correlation_id=%s)",
            request.ticket_id,
            request.correlation_id,
        )

        user_prompt = _build_user_prompt(request)
        t0 = time.monotonic()

        try:
            raw_response = await self._bridge.infer(
                model_key=self._grader_model_key,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                timeout_seconds=self._grader_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "pr-semantic-grading LLM call failed (ticket_id=%s): %s",
                request.ticket_id,
                exc,
            )
            return ModelSemanticGradingResult(
                correlation_id=request.correlation_id,
                ticket_id=request.ticket_id,
                success=False,
                error_code="GRADER_LLM_CALL_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        parsed = _parse_scores(raw_response)

        if parsed is None:
            logger.warning(
                "pr-semantic-grading score parse failed (ticket_id=%s, raw_len=%d)",
                request.ticket_id,
                len(raw_response),
            )
            return ModelSemanticGradingResult(
                correlation_id=request.correlation_id,
                ticket_id=request.ticket_id,
                success=False,
                error_code="GRADER_PARSE_FAILED",
                error_message=(
                    f"Could not extract valid scores from grader response (len={len(raw_response)})"
                ),
            )

        scores, rationale = parsed
        evidence = ModelLLMCallEvidence(
            prompt_template_id=self._prompt_template_id,
            prompt_template_version=self._prompt_template_version,
            grader_model_key=self._grader_model_key,
            latency_ms=latency_ms,
        )

        logger.info(
            "pr-semantic-grading complete (ticket_id=%s, criteria_coverage=%.2f, "
            "contract_alignment=%.2f, anti_pattern_present=%.2f, "
            "overall_confidence=%.2f, latency_ms=%d)",
            request.ticket_id,
            scores["criteria_coverage"],
            scores["contract_alignment"],
            scores["anti_pattern_present"],
            scores["overall_confidence"],
            latency_ms,
        )

        return ModelSemanticGradingResult.with_scores(
            correlation_id=request.correlation_id,
            ticket_id=request.ticket_id,
            criteria_coverage=scores["criteria_coverage"],
            contract_alignment=scores["contract_alignment"],
            anti_pattern_present=scores["anti_pattern_present"],
            overall_confidence=scores["overall_confidence"],
            rationale=rationale,
            llm_call_evidence=evidence,
        )

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> HandlerPrSemanticGrader:
        """Build from a loaded contract.yaml config block."""
        cfg = contract.get("config", {})
        return cls(
            grader_model_key=str(
                cfg.get("grader_model_key", {}).get("default", "opus")
            ),
            grader_timeout_seconds=float(
                cfg.get("grader_timeout_seconds", {}).get("default", 120.0)
            ),
            prompt_template_id=str(
                cfg.get("prompt_template_id", {}).get(
                    "default", "pr_semantic_grading_v1"
                )
            ),
            prompt_template_version=str(
                cfg.get("prompt_template_version", {}).get("default", "1.0.0")
            ),
        )


__all__: list[str] = ["HandlerPrSemanticGrader"]
