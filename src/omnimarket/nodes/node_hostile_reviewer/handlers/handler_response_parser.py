"""Response Parser — normalizes raw LLM output into ModelReviewFinding instances.

Handles: valid JSON arrays, JSON wrapped in markdown fences, legacy format with
{findings: [...]}, plain text bracket extraction. Reports parse_failure and
format_failure per the design doc failure taxonomy.

No I/O. Pure function.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    ModelFindingEvidence,
    ModelReviewFinding,
)

_CATEGORY_MAP: dict[str, EnumFindingCategory] = {
    "security": EnumFindingCategory.SECURITY,
    "logic_error": EnumFindingCategory.LOGIC_ERROR,
    "logic_errors": EnumFindingCategory.LOGIC_ERROR,
    "correctness": EnumFindingCategory.LOGIC_ERROR,
    "integration": EnumFindingCategory.INTEGRATION,
    "scope_violation": EnumFindingCategory.SCOPE_VIOLATION,
    "scope_violations": EnumFindingCategory.SCOPE_VIOLATION,
    "contract_breach": EnumFindingCategory.CONTRACT_BREACH,
    "style": EnumFindingCategory.STYLE,
    "informational": EnumFindingCategory.INFORMATIONAL,
    "architecture": EnumFindingCategory.INTEGRATION,
    "performance": EnumFindingCategory.LOGIC_ERROR,
    "completeness": EnumFindingCategory.SCOPE_VIOLATION,
    "feasibility": EnumFindingCategory.INTEGRATION,
    "testing": EnumFindingCategory.SCOPE_VIOLATION,
}

_SEVERITY_MAP: dict[str, EnumFindingSeverity] = {
    "critical": EnumFindingSeverity.CRITICAL,
    "major": EnumFindingSeverity.MAJOR,
    "minor": EnumFindingSeverity.MINOR,
    "nit": EnumFindingSeverity.NIT,
}

_CONFIDENCE_MAP: dict[str, EnumReviewConfidence] = {
    "high": EnumReviewConfidence.HIGH,
    "medium": EnumReviewConfidence.MEDIUM,
    "low": EnumReviewConfidence.LOW,
}


class EnumParseStatus(StrEnum):
    SUCCESS = "success"
    FORMAT_FAILURE = "format_failure"
    PARSE_FAILURE = "parse_failure"


class ModelParseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumParseStatus = Field(...)
    findings: list[ModelReviewFinding] = Field(default_factory=list)
    error_message: str = Field(default="")
    raw_length: int = Field(default=0)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    text = _strip_markdown_fences(text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "findings" in parsed:
            findings = parsed["findings"]
            if isinstance(findings, list):
                return findings
        return None
    except json.JSONDecodeError:
        # Try bracket extraction
        start = text.find("[")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        result: list[dict[str, Any]] = json.loads(text[start : i + 1])
                        return result
                    except json.JSONDecodeError:
                        return None
        return None


def _normalize_finding(
    raw: dict[str, Any], source_model: str
) -> ModelReviewFinding | None:
    desc = raw.get("description", "")
    if not isinstance(desc, str) or not desc.strip():
        return None

    title = raw.get("title", desc[:80])
    if not isinstance(title, str) or not title.strip():
        title = desc[:80]

    category_raw = str(raw.get("category", "informational")).lower()
    category = _CATEGORY_MAP.get(category_raw, EnumFindingCategory.INFORMATIONAL)

    severity_raw = str(raw.get("severity", "minor")).lower()
    severity = _SEVERITY_MAP.get(severity_raw, EnumFindingSeverity.MINOR)

    confidence_raw = str(raw.get("confidence", "medium")).lower()
    confidence = _CONFIDENCE_MAP.get(confidence_raw, EnumReviewConfidence.MEDIUM)

    evidence = ModelFindingEvidence(
        file_path=raw.get("location") or raw.get("file_path"),
        code_snippet=raw.get("evidence") or raw.get("code_snippet"),
    )

    detection = raw.get("detection_method", "") or raw.get("detection", "") or ""

    return ModelReviewFinding(
        id=uuid4(),
        category=category,
        severity=severity,
        title=title[:120],
        description=desc[:500],
        evidence=evidence,
        confidence=confidence,
        source_model=source_model,
        detection_method=str(detection),
    )


def parse_model_response(raw_text: str, source_model: str) -> ModelParseResult:
    """Parse raw LLM response text into normalized ModelReviewFinding list."""
    if not raw_text or not raw_text.strip():
        return ModelParseResult(
            status=EnumParseStatus.SUCCESS,
            findings=[],
            raw_length=0,
        )

    raw_array = _extract_json_array(raw_text)
    if raw_array is None:
        return ModelParseResult(
            status=EnumParseStatus.FORMAT_FAILURE,
            error_message="Could not extract JSON array from response",
            raw_length=len(raw_text),
        )

    findings: list[ModelReviewFinding] = []
    for item in raw_array:
        if not isinstance(item, dict):
            continue
        finding = _normalize_finding(item, source_model)
        if finding is not None:
            findings.append(finding)

    return ModelParseResult(
        status=EnumParseStatus.SUCCESS,
        findings=findings,
        raw_length=len(raw_text),
    )


class HandlerResponseParser:
    """RuntimeLocal handler protocol wrapper for response parser."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to parse_model_response. Expects input_data with
        'raw_text' and 'source_model' keys.
        """
        raw_text = input_data.get("raw_text")
        if not isinstance(raw_text, str):
            raise TypeError("handle() requires a str in input_data['raw_text']")
        source_model = input_data.get("source_model")
        if not isinstance(source_model, str):
            raise TypeError("handle() requires a str in input_data['source_model']")
        result = parse_model_response(raw_text, source_model)
        return result.model_dump(mode="json")


__all__: list[str] = [
    "EnumParseStatus",
    "HandlerResponseParser",
    "ModelParseResult",
    "parse_model_response",
]
