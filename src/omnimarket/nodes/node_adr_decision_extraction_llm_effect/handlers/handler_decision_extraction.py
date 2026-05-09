# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerDecisionExtraction -- identify architectural decisions from segmented documents.

Sends a structured prompt to a configurable LLM requesting extraction of
decision-type items classified by a six-type taxonomy. Extraction IDs are
deterministically computed from extraction_version, model_id, source segment
IDs, and segment content hashes. On LLM or parse failure, returns success=False
with error details — never an empty success.

JSON repair policy: one re-prompt on invalid JSON. Second failure = failed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
    ModelExtractionRequest,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_result import (
    EnumDecisionType,
    ModelDecisionExtraction,
    ModelExtractionResult,
    ModelLLMCallEvidence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)

logger = logging.getLogger(__name__)

_EXTRACTION_VERSION = "1"
_PROMPT_TEMPLATE_ID = "adr_decision_extraction_v1"
_PROMPT_TEMPLATE_VERSION = "1.0.0"

_DECISION_TYPE_DESCRIPTIONS = {
    EnumDecisionType.architecture_decision: (
        "A resolved architectural or technical decision. "
        'Signal words: "decided", "chose", "selected", "adopted".'
    ),
    EnumDecisionType.architecture_pivot: (
        "A change of direction away from a prior approach. "
        'Signal words: "replaced", "abandoned", "moved away from", "pivoted".'
    ),
    EnumDecisionType.doctrine_formation: (
        "An emerging organizational rule or principle being codified. "
        'Signal words: "must", "never", "always", "invariant", "principle".'
    ),
    EnumDecisionType.operational_lesson: (
        "A lesson learned from an incident or operational failure. "
        'Signal words: "incident", "broke", "learned", "burned".'
    ),
    EnumDecisionType.supersession: (
        "A statement that one thing replaces or deprecates another. "
        'Signal words: "replaces", "supersedes", "deprecated", "obsoletes".'
    ),
    EnumDecisionType.rejected_approach: (
        "An approach that was considered but explicitly not adopted. "
        'Signal words: "rejected", "considered but", "didn\'t work", "abandoned".'
    ),
}

_SYSTEM_PROMPT = (
    "You are an expert architectural decision analyst specializing in extracting "
    "decisions, pivots, doctrine, and lessons from engineering documentation.\n"
    "\n"
    "Extract items from the provided document segments that fit the following taxonomy:\n\n"
    + "\n".join(
        f"- {dt.value}: {desc}" for dt, desc in _DECISION_TYPE_DESCRIPTIONS.items()
    )
    + "\n\n"
    "Rules:\n"
    "- Only extract items with clear textual evidence in the segments provided.\n"
    "- Each extraction must cite at least one source_segment_id.\n"
    "- Provide verbatim evidence_quotes from the source text.\n"
    "- confidence must reflect how clearly the text supports the classification (0.0-1.0).\n"
    "- Do NOT extract items with confidence < 0.3.\n"
    "- An empty array [] is valid if no qualifying items are found.\n"
    "\n"
    "Respond with ONLY a JSON array. Each element must have exactly these fields:\n"
    '  "decision_type": one of the taxonomy values above,\n'
    '  "statement": concise statement of the decision/pivot/lesson/doctrine,\n'
    '  "rationale": supporting rationale from the source text (string or null),\n'
    '  "source_segment_ids": list of segment_id strings that evidence this extraction,\n'
    '  "evidence_quotes": list of verbatim quote strings from the source,\n'
    '  "confidence": float 0.0-1.0\n'
    "\n"
    "No prose, no markdown fences, no explanation — only the JSON array."
)

_JSON_REPAIR_PROMPT = (
    "Your previous response was not valid JSON. "
    "Return ONLY the JSON array described in the system prompt — "
    "no prose, no markdown fences, no explanation."
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compute_extraction_id(
    model_id: str,
    source_segment_ids: list[str],
    segment_content_hashes: list[str],
) -> str:
    key = (
        _EXTRACTION_VERSION
        + "|"
        + model_id
        + "|"
        + ",".join(sorted(source_segment_ids))
        + "|"
        + ",".join(sorted(segment_content_hashes))
    )
    return _sha256(key)


def _build_user_prompt(request: ModelExtractionRequest) -> str:
    segment_texts = []
    for seg in request.segments:
        segment_texts.append(
            f"[segment_id={seg.segment_id} type={seg.segment_type} "
            f"lines={seg.start_line}-{seg.end_line}]\n{seg.content}"
        )
    combined = "\n\n---\n\n".join(segment_texts)
    return (
        f"## Source Document: {request.source_path}\n\n"
        f"{combined}\n\n"
        "Extract all qualifying items from the segments above."
    )


def _parse_extractions(raw: str) -> list[dict[str, Any]] | None:
    """Parse raw LLM response into a list of extraction dicts. Returns None on failure."""
    text = raw.strip()
    # Strip optional markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return data


def _validate_item(item: Any) -> bool:
    """Check that a raw dict has the required fields for an extraction."""
    if not isinstance(item, dict):
        return False
    required = {"decision_type", "statement", "source_segment_ids", "confidence"}
    if not required.issubset(item.keys()):
        return False
    try:
        EnumDecisionType(str(item["decision_type"]))
    except ValueError:
        return False
    if (
        not isinstance(item["source_segment_ids"], list)
        or not item["source_segment_ids"]
    ):
        return False
    conf = item.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return False
    return 0.0 <= float(conf) <= 1.0


class HandlerDecisionExtraction:
    """Decision extraction handler for ADR canary pipeline.

    Inject ``inference_bridge`` in tests to avoid real network calls.
    Config is read from the contract; only endpoint credentials remain in env.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        extraction_timeout_seconds: float = 120.0,
        prompt_template_id: str = _PROMPT_TEMPLATE_ID,
        prompt_template_version: str = _PROMPT_TEMPLATE_VERSION,
    ) -> None:
        self._bridge: ModelInferenceAdapter = (
            inference_bridge or AdapterInferenceBridge(ModelInferenceBridgeConfig())
        )
        self._extraction_timeout_seconds = extraction_timeout_seconds
        self._prompt_template_id = prompt_template_id
        self._prompt_template_version = prompt_template_version

    async def handle(self, request: ModelExtractionRequest) -> ModelExtractionResult:
        logger.info(
            "adr-decision-extraction started (model_key=%s, source=%s, correlation_id=%s)",
            request.model_key,
            request.source_path,
            request.correlation_id,
        )

        segment_id_map = {seg.segment_id: seg for seg in request.segments}
        user_prompt = _build_user_prompt(request)
        t0 = time.monotonic()
        json_repair_attempted = False

        # Resolve model_id for deterministic extraction_id computation
        model_id = request.model_key  # fallback; bridge resolves actual ID at call time

        try:
            raw_response = await self._bridge.infer(
                model_key=request.model_key,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                timeout_seconds=self._extraction_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "adr-decision-extraction LLM call failed (model_key=%s, source=%s): %s",
                request.model_key,
                request.source_path,
                exc,
            )
            return ModelExtractionResult(
                correlation_id=request.correlation_id,
                source_path=request.source_path,
                model_key=request.model_key,
                success=False,
                error_code="EXTRACTION_LLM_CALL_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
                model_id=model_id,
                retryable=True,
            )

        raw_items = _parse_extractions(raw_response)

        if raw_items is None:
            # One repair attempt
            json_repair_attempted = True
            logger.info(
                "adr-decision-extraction JSON repair attempted (model_key=%s)",
                request.model_key,
            )
            try:
                repaired_response = await self._bridge.infer(
                    model_key=request.model_key,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=_JSON_REPAIR_PROMPT,
                    timeout_seconds=self._extraction_timeout_seconds,
                )
            except Exception as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ModelExtractionResult(
                    correlation_id=request.correlation_id,
                    source_path=request.source_path,
                    model_key=request.model_key,
                    success=False,
                    error_code="EXTRACTION_REPAIR_LLM_FAILED",
                    error_message=f"{type(exc).__name__}: {exc}",
                    model_id=model_id,
                    retryable=True,
                    llm_call_evidence=ModelLLMCallEvidence(
                        prompt_template_id=self._prompt_template_id,
                        prompt_template_version=self._prompt_template_version,
                        extraction_model_key=request.model_key,
                        extraction_model_id=model_id,
                        latency_ms=latency_ms,
                        json_repair_attempted=True,
                    ),
                )
            raw_items = _parse_extractions(repaired_response)

        latency_ms = int((time.monotonic() - t0) * 1000)

        if raw_items is None:
            logger.warning(
                "adr-decision-extraction parse failed after repair (model_key=%s, source=%s)",
                request.model_key,
                request.source_path,
            )
            return ModelExtractionResult(
                correlation_id=request.correlation_id,
                source_path=request.source_path,
                model_key=request.model_key,
                success=False,
                error_code="EXTRACTION_PARSE_FAILED",
                error_message="Could not parse JSON extraction array after repair attempt.",
                model_id=model_id,
                retryable=False,
                llm_call_evidence=ModelLLMCallEvidence(
                    prompt_template_id=self._prompt_template_id,
                    prompt_template_version=self._prompt_template_version,
                    extraction_model_key=request.model_key,
                    extraction_model_id=model_id,
                    latency_ms=latency_ms,
                    json_repair_attempted=json_repair_attempted,
                ),
            )

        # Build ModelDecisionExtraction instances
        extractions: list[ModelDecisionExtraction] = []
        for item in raw_items:
            if not _validate_item(item):
                logger.debug("adr-decision-extraction skipping invalid item: %r", item)
                continue

            seg_ids: list[str] = [str(s) for s in item["source_segment_ids"]]
            content_hashes = [
                _sha256(segment_id_map[sid].content)
                for sid in seg_ids
                if sid in segment_id_map
            ]
            if not content_hashes:
                # Fallback: hash the segment IDs themselves if segments not in map
                content_hashes = [_sha256(sid) for sid in seg_ids]

            extraction_id = _compute_extraction_id(
                model_id=model_id,
                source_segment_ids=seg_ids,
                segment_content_hashes=content_hashes,
            )

            quotes = item.get("evidence_quotes", [])
            if not isinstance(quotes, list):
                quotes = []

            extractions.append(
                ModelDecisionExtraction(
                    extraction_id=extraction_id,
                    decision_type=EnumDecisionType(str(item["decision_type"])),
                    statement=str(item["statement"]),
                    rationale=str(item["rationale"]) if item.get("rationale") else None,
                    source_segment_ids=seg_ids,
                    evidence_quotes=[str(q) for q in quotes],
                    extraction_model_id=model_id,
                    prompt_template_id=self._prompt_template_id,
                    prompt_template_version=self._prompt_template_version,
                    confidence=float(item["confidence"]),
                )
            )

        evidence = ModelLLMCallEvidence(
            prompt_template_id=self._prompt_template_id,
            prompt_template_version=self._prompt_template_version,
            extraction_model_key=request.model_key,
            extraction_model_id=model_id,
            latency_ms=latency_ms,
            json_repair_attempted=json_repair_attempted,
        )

        logger.info(
            "adr-decision-extraction complete (model_key=%s, source=%s, "
            "extractions=%d, latency_ms=%d)",
            request.model_key,
            request.source_path,
            len(extractions),
            latency_ms,
        )

        return ModelExtractionResult(
            correlation_id=request.correlation_id,
            source_path=request.source_path,
            model_key=request.model_key,
            success=True,
            extractions=extractions,
            llm_call_evidence=evidence,
        )

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> HandlerDecisionExtraction:
        """Build from a loaded contract.yaml config block."""
        cfg = contract.get("config", {})
        return cls(
            extraction_timeout_seconds=float(
                cfg.get("extraction_timeout_seconds", {}).get("default", 120.0)
            ),
            prompt_template_id=str(
                cfg.get("prompt_template_id", {}).get("default", _PROMPT_TEMPLATE_ID)
            ),
            prompt_template_version=str(
                cfg.get("prompt_template_version", {}).get(
                    "default", _PROMPT_TEMPLATE_VERSION
                )
            ),
        )


__all__: list[str] = ["HandlerDecisionExtraction"]
