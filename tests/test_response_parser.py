"""Tests for LLM response parser — normalizes raw model output to ModelReviewFinding."""

import json

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    EnumParseStatus,
    parse_model_response,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
)


def test_valid_json_array():
    raw = json.dumps(
        [
            {
                "category": "security",
                "severity": "critical",
                "title": "SQL injection",
                "description": "Unsanitized input",
                "evidence": "line 42",
                "proposed_fix": "Use parameterized queries",
                "location": "src/db.py",
            }
        ]
    )
    result = parse_model_response(raw, source_model="test-model")
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].category == EnumFindingCategory.SECURITY


def test_empty_array_is_clean():
    result = parse_model_response("[]", source_model="test-model")
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


def test_malformed_json_returns_format_failure():
    result = parse_model_response("not json at all", source_model="test-model")
    assert result.status == EnumParseStatus.FORMAT_FAILURE


def test_json_wrapped_in_markdown_fences():
    raw = '```json\n[{"category": "style", "severity": "nit", "title": "Naming", "description": "Bad name", "evidence": "", "proposed_fix": "Rename", "location": null}]\n```'
    result = parse_model_response(raw, source_model="test-model")
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1


def test_legacy_findings_format():
    """The legacy format from aggregate_reviews.py uses description/confidence/detection."""
    raw = json.dumps(
        {
            "findings": [
                {
                    "description": "Some issue",
                    "confidence": "high",
                    "detection": "review",
                }
            ]
        }
    )
    result = parse_model_response(raw, source_model="legacy-model")
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
