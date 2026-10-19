# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict parse of a v2 compact review verdict (compact_strict mode).

No tolerance and no salvage: the reply is the verdict JSON object, byte for
byte. Any of these gives a failed result with no findings:

- a provider stop reason other than ``tool_use``, ``end_turn`` or ``stop``;
- JSON that does not decode (a verdict cut mid-array);
- a verdict that does not fit the schema, ``done`` absent or false included;
- ``more: true`` (the voter had more findings than it could list);
- a catalog code outside the supplied catalog, an ``X`` or ``R:`` finding
  without a family, an ``X`` finding without a note, or an ``R:`` id that is
  not one of the standing rules supplied to the review.

A mandatory ``R:`` finding comes out critical. Error messages name the rule
that failed and never echo reply text. Pure; no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    ModelFindingEvidence,
    ModelReviewFinding,
)
from omnimarket.nodes.node_review_response_parser_compute.models.model_compact_verdict import (
    ModelCompactFinding,
    ModelCompactVerdict,
)
from omnimarket.review.response_parser import (
    EnumParseFailureKind,
    EnumParseStatus,
    ModelParseResult,
)

NORMAL_STOP_REASONS: frozenset[str] = frozenset({"tool_use", "end_turn", "stop"})
"""Normal ends: Messages ``tool_use``/``end_turn``, OpenAI-compatible ``stop``."""

_UNCODED = "X"
_RULE_PREFIX = "R:"
_DETECTION_METHOD = "compact_strict"

FAMILY_CATEGORY: dict[str, EnumFindingCategory] = {
    "architecture": EnumFindingCategory.INTEGRATION,
    "security": EnumFindingCategory.SECURITY,
    "performance": EnumFindingCategory.LOGIC_ERROR,
    "correctness": EnumFindingCategory.LOGIC_ERROR,
    "completeness": EnumFindingCategory.SCOPE_VIOLATION,
    "feasibility": EnumFindingCategory.INTEGRATION,
    "testing": EnumFindingCategory.SCOPE_VIOLATION,
    "style": EnumFindingCategory.STYLE,
}

_SEVERITY: dict[str, EnumFindingSeverity] = {
    "C": EnumFindingSeverity.CRITICAL,
    "M": EnumFindingSeverity.MAJOR,
    "m": EnumFindingSeverity.MINOR,
    "n": EnumFindingSeverity.NIT,
}


def _fail(
    kind: EnumParseFailureKind, message: str, raw_length: int
) -> ModelParseResult:
    status = (
        EnumParseStatus.PARSE_FAILURE
        if kind == EnumParseFailureKind.TRUNCATED
        else EnumParseStatus.FORMAT_FAILURE
    )
    return ModelParseResult(
        status=status,
        failure_kind=kind,
        findings=[],
        error_message=message,
        raw_length=raw_length,
    )


def _finding_error(
    item: ModelCompactFinding,
    known_codes: Mapping[str, str],
    standing_rules: Mapping[str, bool],
) -> str | None:
    """Return the first semantic violation of one finding, or None."""
    if item.c == _UNCODED:
        if item.y is None:
            return "X finding has no family"
        if not item.n:
            return "X finding has no note"
        return None
    if item.c.startswith(_RULE_PREFIX):
        if item.c[len(_RULE_PREFIX) :] not in standing_rules:
            return "R: finding names a standing rule that was not supplied"
        if item.y is None:
            return "R: finding has no family"
        return None
    if item.c not in known_codes:
        return "finding cites a code outside the catalog"
    if item.y is not None:
        return "catalog finding carries a family; the catalog fixes it"
    return None


def _to_finding(
    item: ModelCompactFinding,
    source_model: str,
    known_codes: Mapping[str, str],
    standing_rules: Mapping[str, bool],
) -> ModelReviewFinding:
    family = item.y if item.y is not None else known_codes[item.c]
    severity = _SEVERITY[item.s]
    if item.c.startswith(_RULE_PREFIX) and standing_rules[item.c[len(_RULE_PREFIX) :]]:
        severity = EnumFindingSeverity.CRITICAL
    category = FAMILY_CATEGORY.get(family, EnumFindingCategory.INFORMATIONAL)
    note = item.n or ""
    return ModelReviewFinding(
        id=uuid4(),
        category=category,
        severity=severity,
        title=item.c,
        description=f"{item.c}: {note}" if note else item.c,
        evidence=ModelFindingEvidence(
            file_path=item.p,
            line_range={"start": item.line, "end": item.line}
            if item.line > 0
            else None,
        ),
        confidence=EnumReviewConfidence.MEDIUM,
        source_model=source_model,
        detection_method=_DETECTION_METHOD,
        defect_code=item.c,
    )


def parse_compact_strict(
    raw_text: str,
    *,
    source_model: str,
    stop_reason: str | None,
    known_codes: Mapping[str, str],
    standing_rules: Mapping[str, bool],
    max_findings: int,
) -> ModelParseResult:
    """Parse one compact verdict; any violation is a failed result, no findings."""
    raw_length = len(raw_text)
    if stop_reason is not None and stop_reason not in NORMAL_STOP_REASONS:
        return _fail(
            EnumParseFailureKind.TRUNCATED,
            f"provider stop reason {stop_reason!r} is not a normal end",
            raw_length,
        )
    text = raw_text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # A verdict is a JSON object; one that opens and does not close was cut.
        kind = (
            EnumParseFailureKind.TRUNCATED
            if text.startswith("{")
            else EnumParseFailureKind.SCHEMA_INVALID
        )
        return _fail(kind, "reply is not a complete JSON verdict", raw_length)
    try:
        verdict = ModelCompactVerdict.model_validate(payload)
    except ValidationError:
        return _fail(
            EnumParseFailureKind.SCHEMA_INVALID,
            "reply does not fit the compact verdict schema",
            raw_length,
        )
    if verdict.more:
        return _fail(
            EnumParseFailureKind.OVERFLOW,
            "voter reported more findings than it listed",
            raw_length,
        )
    if len(verdict.f) > max_findings:
        return _fail(
            EnumParseFailureKind.SCHEMA_INVALID,
            f"verdict lists more than {max_findings} findings",
            raw_length,
        )
    for item in verdict.f:
        error = _finding_error(item, known_codes, standing_rules)
        if error is not None:
            return _fail(EnumParseFailureKind.SCHEMA_INVALID, error, raw_length)
    return ModelParseResult(
        status=EnumParseStatus.SUCCESS,
        findings=[
            _to_finding(item, source_model, known_codes, standing_rules)
            for item in verdict.f
        ],
        raw_length=raw_length,
    )


__all__: list[str] = ["FAMILY_CATEGORY", "NORMAL_STOP_REASONS", "parse_compact_strict"]
