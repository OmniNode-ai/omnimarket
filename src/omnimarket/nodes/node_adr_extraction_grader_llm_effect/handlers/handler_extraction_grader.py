# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerExtractionGrader -- heuristic grader for ADR extraction quality.

Uses Claude Opus (or any configured grader model) to score extraction output
against a ground-truth ADR across four dimensions: recall, precision, fidelity,
and format_compliance.

Scores are heuristic evaluations for ranking model behavior — not ground truth
determination. On grader failure, returns success=False with error details
rather than zero scores (which would be indistinguishable from a genuinely
poor extraction).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal

from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request import (
    ModelGradingRequest,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_result import (
    ModelGradingResult,
    ModelLLMCallEvidence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert evaluator of architectural decision record (ADR) extraction systems.\n"
    "\n"
    "IMPORTANT: This is heuristic evaluation for ranking model behavior, not ground truth "
    "determination. Your scores help compare extraction models — they are not authoritative "
    "judgements on the correctness of the ADR content itself.\n"
    "\n"
    "Evaluate the extraction output against the ground truth ADR on exactly four dimensions:\n"
    "\n"
    "- recall: fraction of decisions/content from the ground truth that appear in the extraction "
    "(0.0 = nothing captured, 1.0 = everything captured)\n"
    "- precision: fraction of extracted items that are accurate and relevant vs. the ground truth "
    "(0.0 = all noise, 1.0 = perfectly accurate)\n"
    "- fidelity: semantic faithfulness — does the extraction preserve the meaning and intent of the "
    "original source without distortion or hallucination? (0.0 = severely distorted, 1.0 = faithful)\n"
    "- format_compliance: does the extraction conform to the expected structured output schema? "
    "(0.0 = completely wrong format, 1.0 = perfect schema adherence)\n"
    "\n"
    "Respond with ONLY a JSON object in this exact format, no prose:\n"
    '{"recall": <float>, "precision": <float>, "fidelity": <float>, "format_compliance": <float>, '
    '"rationale": "<one sentence per dimension separated by |>"}\n'
)


def _build_user_prompt(request: ModelGradingRequest) -> str:
    extraction_json = json.dumps(request.extraction_output, indent=2)
    return (
        f"## Ground Truth ADR\n\n{request.ground_truth_adr}\n\n"
        f"## Source Document\n\n{request.source_document}\n\n"
        f"## Extraction Output (model under test: {request.model_key_under_test})\n\n"
        f"```json\n{extraction_json}\n```\n\n"
        "Score the extraction output against the ground truth ADR on the four dimensions."
    )


def _parse_scores(raw: str) -> dict[str, float] | None:
    """Extract JSON scores from Opus response. Returns None on parse failure."""
    match = re.search(r"\{[^}]+\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    required = {"recall", "precision", "fidelity", "format_compliance"}
    if not required.issubset(data.keys()):
        return None

    scores: dict[str, float] = {}
    for key in required:
        try:
            val = float(data[key])
        except (TypeError, ValueError):
            return None
        if not (0.0 <= val <= 1.0):
            return None
        scores[key] = val

    return scores


class HandlerExtractionGrader:
    """Heuristic grader for ADR extraction quality using a configurable LLM.

    Inject ``inference_bridge`` in tests to avoid real network calls.
    Config is read from the contract; only the API key remains in env.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        grader_model_key: str = "opus",
        grader_timeout_seconds: float = 120.0,
        prompt_template_id: str = "adr_grading_v1",
        prompt_template_version: str = "1.0.0",
    ) -> None:
        self._bridge: ModelInferenceAdapter = (
            inference_bridge or AdapterInferenceBridge(ModelInferenceBridgeConfig())
        )
        self._grader_model_key = grader_model_key
        self._grader_timeout_seconds = grader_timeout_seconds
        self._prompt_template_id = prompt_template_id
        self._prompt_template_version = prompt_template_version

    async def handle(self, request: ModelGradingRequest) -> ModelGradingResult:
        logger.info(
            "adr-grading started (model_under_test=%s, correlation_id=%s)",
            request.model_key_under_test,
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
                "adr-grading LLM call failed (model_under_test=%s): %s",
                request.model_key_under_test,
                exc,
            )
            return ModelGradingResult(
                correlation_id=request.correlation_id,
                model_key_under_test=request.model_key_under_test,
                success=False,
                error_code="GRADER_LLM_CALL_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        scores = _parse_scores(raw_response)

        if scores is None:
            logger.warning(
                "adr-grading score parse failed (model_under_test=%s, raw_len=%d)",
                request.model_key_under_test,
                len(raw_response),
            )
            return ModelGradingResult(
                correlation_id=request.correlation_id,
                model_key_under_test=request.model_key_under_test,
                success=False,
                error_code="GRADER_PARSE_FAILED",
                error_message=(
                    f"Could not extract valid scores from grader response (len={len(raw_response)})"
                ),
            )

        evidence = ModelLLMCallEvidence(
            prompt_template_id=self._prompt_template_id,
            prompt_template_version=self._prompt_template_version,
            grader_model_key=self._grader_model_key,
            latency_ms=latency_ms,
        )

        logger.info(
            "adr-grading complete (model_under_test=%s, recall=%.2f, precision=%.2f, "
            "fidelity=%.2f, format_compliance=%.2f, latency_ms=%d)",
            request.model_key_under_test,
            scores["recall"],
            scores["precision"],
            scores["fidelity"],
            scores["format_compliance"],
            latency_ms,
        )

        return ModelGradingResult(
            correlation_id=request.correlation_id,
            model_key_under_test=request.model_key_under_test,
            success=True,
            recall=scores["recall"],
            precision=scores["precision"],
            fidelity=scores["fidelity"],
            format_compliance=scores["format_compliance"],
            llm_call_evidence=evidence,
        )

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> HandlerExtractionGrader:
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
                cfg.get("prompt_template_id", {}).get("default", "adr_grading_v1")
            ),
            prompt_template_version=str(
                cfg.get("prompt_template_version", {}).get("default", "1.0.0")
            ),
        )


__all__: list[str] = ["HandlerExtractionGrader"]
