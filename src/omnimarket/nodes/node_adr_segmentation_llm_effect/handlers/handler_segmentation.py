# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerSegmentation -- semantic segmentation of markdown documents via LLM.

Sends a structured prompt to an LLM requesting the document be split into
semantic units classified by type. Segment IDs and content hashes are
deterministically computed from the source. On LLM failure the result carries
success=False with error details — never an empty success.

JSON repair policy: one re-prompt on invalid JSON. Second failure sets
format_compliance=0 and extraction_failed in evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_request import (
    ModelSegmentationRequest,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_result import (
    EnumSegmentType,
    ModelDocumentSegment,
    ModelLLMCallEvidence,
    ModelSegmentationResult,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
)

logger = logging.getLogger(__name__)

_SEGMENT_TYPES = [t.value for t in EnumSegmentType]

_SYSTEM_PROMPT_TEMPLATE = (
    "You are an expert technical document analyst specializing in architectural decision records "
    "(ADRs) and engineering documentation.\n"
    "\n"
    "Your task is to segment a markdown document into semantic units and classify each unit.\n"
    "\n"
    "Classification types:\n"
    "- decision: A resolved architectural or technical decision with clear rationale\n"
    "- critique: Analysis of problems, anti-patterns, or deficiencies in existing approaches\n"
    "- proposal: A suggested approach, recommendation, or candidate solution not yet decided\n"
    "- migration: A plan or step for transitioning from one state to another\n"
    "- invariant: A rule, constraint, or principle that must hold across the system\n"
    "- failure_analysis: Root cause analysis, post-mortems, or analysis of failures\n"
    "- operational_concern: Operational, deployment, monitoring, or reliability considerations\n"
    "- hypothesis: An unvalidated assumption or theory requiring evidence\n"
    "- doctrine_formation: Emerging patterns being established as organizational doctrine\n"
    "- implementation_detail: Concrete implementation specifics without architectural significance\n"
    "- architectural_risk: Identified risks to architectural integrity or system properties\n"
    "- non_decision: Explicitly deferred, rejected, or out-of-scope items\n"
    "- background: Context, history, or setup that motivates decisions\n"
    "- unknown: Cannot be confidently classified into any above category\n"
    "\n"
    "IMPORTANT: Use 'unknown' as the default for low-confidence classifications "
    "(confidence < {low_confidence_threshold}). Do NOT use 'implementation_detail' "
    "as a catch-all.\n"
    "\n"
    "Respond with ONLY a JSON array of segment objects. Each object must have:\n"
    '  "start_line": integer (1-based),\n'
    '  "end_line": integer (1-based, inclusive),\n'
    '  "segment_type": one of the types listed above,\n'
    '  "content": verbatim text of that line range,\n'
    '  "confidence": float 0.0-1.0\n'
    "\n"
    "No prose, no markdown fences, no explanation — only the JSON array."
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compute_segment_id(
    source_path: str,
    source_content_sha256: str,
    start_line: int,
    end_line: int,
    segment_type: str,
) -> str:
    key = (
        f"{source_path}:{source_content_sha256}:{start_line}:{end_line}:{segment_type}"
    )
    return _sha256(key)


def _build_user_prompt(request: ModelSegmentationRequest) -> str:
    lines = request.source_content.splitlines()
    numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    return (
        f"## Document: {request.source_path}\n\n"
        f"```\n{numbered}\n```\n\n"
        "Segment the above document according to the instructions."
    )


def _parse_segments(
    raw: str,
    request: ModelSegmentationRequest,
    low_confidence_threshold: float,
) -> list[ModelDocumentSegment] | None:
    """Parse LLM JSON response into validated ModelDocumentSegment list.

    Returns None if the JSON is structurally invalid or missing required fields.
    Low-confidence segments have their type overridden to UNKNOWN.
    """
    text = raw.strip()
    # Strip markdown fences if the model wraps in them despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None

    total_lines = len(request.source_content.splitlines())
    segments: list[ModelDocumentSegment] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        required_keys = {
            "start_line",
            "end_line",
            "segment_type",
            "content",
            "confidence",
        }
        if not required_keys.issubset(item.keys()):
            return None

        try:
            start_line = int(item["start_line"])
            end_line = int(item["end_line"])
            confidence = float(item["confidence"])
            content = str(item["content"])
            raw_type = str(item["segment_type"])
        except (TypeError, ValueError):
            return None

        if start_line < 1 or end_line < start_line or end_line > total_lines:
            return None
        if not (0.0 <= confidence <= 1.0):
            return None

        # Validate type, fall back to unknown for unrecognised values
        if raw_type not in _SEGMENT_TYPES:
            raw_type = EnumSegmentType.unknown.value

        # Override low-confidence to unknown per policy
        if confidence < low_confidence_threshold:
            raw_type = EnumSegmentType.unknown.value

        segment_type = EnumSegmentType(raw_type)
        segment_id = _compute_segment_id(
            request.source_path,
            request.source_content_sha256,
            start_line,
            end_line,
            segment_type.value,
        )

        segments.append(
            ModelDocumentSegment(
                segment_id=segment_id,
                source_path=request.source_path,
                source_content_sha256=request.source_content_sha256,
                start_line=start_line,
                end_line=end_line,
                segment_type=segment_type,
                content=content,
                segment_content_sha256=_sha256(content),
                confidence=confidence,
            )
        )

    return segments


class HandlerSegmentation:
    """Semantic segmentation handler using a configurable LLM.

    Inject ``inference_bridge`` in tests to avoid real network calls.
    Config is read from the contract; only endpoint secrets remain in env.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        segmentation_model_key: str = "qwen3-coder",
        segmentation_temperature: float = 0.1,
        segmentation_timeout_seconds: float = 120.0,
        low_confidence_threshold: float = 0.4,
        prompt_template_id: str = "adr_segmentation_v1",
        prompt_template_version: str = "1.0.0",
    ) -> None:
        if inference_bridge is None:
            bridge_config = load_inference_bridge_config_from_env()
            self._bridge = AdapterInferenceBridge(bridge_config)
        else:
            self._bridge = inference_bridge
        self._model_key = segmentation_model_key
        self._temperature = segmentation_temperature
        self._timeout = segmentation_timeout_seconds
        self._low_confidence_threshold = low_confidence_threshold
        self._prompt_template_id = prompt_template_id
        self._prompt_template_version = prompt_template_version

    async def handle(
        self, request: ModelSegmentationRequest
    ) -> ModelSegmentationResult:
        logger.info(
            "adr-segmentation started (source_path=%s, correlation_id=%s)",
            request.source_path,
            request.correlation_id,
        )

        user_prompt = _build_user_prompt(request)
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            low_confidence_threshold=self._low_confidence_threshold
        )
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        prompt_hash = _sha256(full_prompt)
        input_hash = _sha256(request.source_content)

        t0 = time.monotonic()
        raw_response: str | None = None
        json_repair_attempted = False

        try:
            raw_response = await self._bridge.infer(
                model_key=self._model_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_seconds=self._timeout,
                temperature=self._temperature,
            )
        except Exception as exc:
            logger.warning(
                "adr-segmentation LLM call failed (source_path=%s): %s",
                request.source_path,
                exc,
            )
            retryable = isinstance(exc, (TimeoutError, ConnectionError))
            return ModelSegmentationResult(
                correlation_id=request.correlation_id,
                source_path=request.source_path,
                success=False,
                error_code="SEGMENTATION_LLM_CALL_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
                retryable=retryable,
                model_id=self._model_key,
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_hash = _sha256(raw_response)

        segments = _parse_segments(
            raw_response, request, self._low_confidence_threshold
        )

        if segments is None:
            # JSON repair: one re-prompt with error context
            json_repair_attempted = True
            repair_prompt = (
                f"Your previous response could not be parsed as a JSON array. "
                f"Error: invalid JSON or missing required fields. "
                f"Please respond with ONLY a valid JSON array using the schema specified. "
                f"Original document:\n\n{user_prompt}"
            )
            try:
                repair_response = await self._bridge.infer(
                    model_key=self._model_key,
                    system_prompt=system_prompt,
                    user_prompt=repair_prompt,
                    timeout_seconds=self._timeout,
                    temperature=self._temperature,
                )
                segments = _parse_segments(
                    repair_response, request, self._low_confidence_threshold
                )
                if segments is not None:
                    raw_response = repair_response
                    response_hash = _sha256(repair_response)
            except Exception as exc:
                logger.warning(
                    "adr-segmentation repair LLM call failed (source_path=%s): %s",
                    request.source_path,
                    exc,
                )
                segments = None

        evidence = ModelLLMCallEvidence(
            prompt_template_id=self._prompt_template_id,
            prompt_template_version=self._prompt_template_version,
            model_key=self._model_key,
            prompt_hash=prompt_hash,
            input_hash=input_hash,
            response_hash=response_hash,
            latency_ms=latency_ms,
            json_repair_attempted=json_repair_attempted,
        )

        if segments is None:
            logger.warning(
                "adr-segmentation parse failed after %s (source_path=%s)",
                "repair attempt" if json_repair_attempted else "first attempt",
                request.source_path,
            )
            return ModelSegmentationResult(
                correlation_id=request.correlation_id,
                source_path=request.source_path,
                success=False,
                error_code="SEGMENTATION_PARSE_FAILED",
                error_message="JSON parse failed after repair attempt"
                if json_repair_attempted
                else "JSON parse failed on first attempt",
                model_id=self._model_key,
                llm_call_evidence=evidence,
            )

        logger.info(
            "adr-segmentation complete (source_path=%s, segments=%d, latency_ms=%d)",
            request.source_path,
            len(segments),
            latency_ms,
        )

        return ModelSegmentationResult(
            correlation_id=request.correlation_id,
            source_path=request.source_path,
            success=True,
            segments=segments,
            model_id=self._model_key,
            llm_call_evidence=evidence,
        )

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> HandlerSegmentation:
        """Build from a loaded contract.yaml config block."""
        cfg = contract.get("config", {})
        return cls(
            segmentation_model_key=str(
                cfg.get("segmentation_model_key", {}).get("default", "qwen3-coder")
            ),
            segmentation_temperature=float(
                cfg.get("segmentation_temperature", {}).get("default", 0.1)
            ),
            segmentation_timeout_seconds=float(
                cfg.get("segmentation_timeout_seconds", {}).get("default", 120.0)
            ),
            low_confidence_threshold=float(
                cfg.get("low_confidence_threshold", {}).get("default", 0.4)
            ),
            prompt_template_id=str(
                cfg.get("prompt_template_id", {}).get("default", "adr_segmentation_v1")
            ),
            prompt_template_version=str(
                cfg.get("prompt_template_version", {}).get("default", "1.0.0")
            ),
        )


__all__: list[str] = ["HandlerSegmentation"]
