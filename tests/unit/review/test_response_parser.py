# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket.review.response_parser — edge-case and failure branches.

Covers branches not exercised by tests/test_response_parser.py:
  - parse_model_response(): empty / whitespace-only input → SUCCESS with no findings
  - parse_model_response(): non-dict array item is silently skipped
  - _normalize_finding(): non-string or empty description → finding dropped
  - _normalize_finding(): non-string or empty title falls back to desc[:80]
  - _extract_json_array(): bracket extraction state machine (string state, escape state)
  - _extract_json_array(): bracket found but inner JSON fails → FORMAT_FAILURE
  - _extract_json_array(): unclosed bracket (depth never returns to zero) → FORMAT_FAILURE
"""

from __future__ import annotations

import json

import pytest

from omnimarket.review.response_parser import (
    EnumParseStatus,
    parse_model_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODEL = "test-model"


def _one_finding(**kwargs: object) -> str:
    """Return a JSON array string containing a single finding dict."""
    base: dict[str, object] = {
        "title": "Title",
        "description": "A meaningful description here",
        "category": "style",
        "severity": "nit",
    }
    base.update(kwargs)
    return json.dumps([base])


# ---------------------------------------------------------------------------
# parse_model_response() — empty / whitespace input
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_string_returns_success_no_findings() -> None:
    """Empty string → SUCCESS branch (lines 176-181 in response_parser.py)."""
    result = parse_model_response("", source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []
    assert result.raw_length == 0


@pytest.mark.unit
def test_whitespace_only_returns_success_no_findings() -> None:
    """Whitespace-only string is treated the same as empty input."""
    result = parse_model_response("   \n\t  ", source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []
    assert result.raw_length == 0


# ---------------------------------------------------------------------------
# parse_model_response() — non-dict array items are skipped (line 193-194)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_dict_array_item_is_skipped() -> None:
    """Non-dict items in the parsed array are silently skipped."""
    raw = json.dumps(
        [
            "a plain string",
            42,
            None,
            {
                "title": "Real finding",
                "description": "A valid description",
                "category": "security",
                "severity": "major",
            },
        ]
    )
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == "Real finding"


@pytest.mark.unit
def test_all_non_dict_items_produces_empty_findings() -> None:
    """Array of only non-dict items → SUCCESS with zero findings."""
    raw = json.dumps(["string", 1, True, None])
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


# ---------------------------------------------------------------------------
# _normalize_finding() — description must be a non-empty string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_string_description_drops_finding() -> None:
    """description that is not a str causes the finding to be dropped (line 139)."""
    raw = json.dumps(
        [{"title": "T", "description": 42, "category": "style", "severity": "nit"}]
    )
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


@pytest.mark.unit
def test_none_description_drops_finding() -> None:
    """description=None (not a str) causes the finding to be dropped."""
    raw = json.dumps(
        [{"title": "T", "description": None, "category": "style", "severity": "nit"}]
    )
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


@pytest.mark.unit
def test_empty_description_drops_finding() -> None:
    """Empty string description causes the finding to be dropped (line 139)."""
    raw = _one_finding(description="")
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


@pytest.mark.unit
def test_whitespace_description_drops_finding() -> None:
    """Whitespace-only description is treated as empty → finding dropped."""
    raw = _one_finding(description="   ")
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


@pytest.mark.unit
def test_missing_description_drops_finding() -> None:
    """No description key at all → raw.get('description', '') returns '' → dropped."""
    raw = json.dumps([{"title": "T", "category": "style", "severity": "nit"}])
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


# ---------------------------------------------------------------------------
# _normalize_finding() — title falls back to desc[:80] (lines 141-143)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_string_title_falls_back_to_description_prefix() -> None:
    """Non-string title is replaced by desc[:80] (line 143)."""
    desc = "Descriptive text that serves as the title fallback"
    raw = _one_finding(title=999, description=desc)
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == desc[:80]


@pytest.mark.unit
def test_empty_title_falls_back_to_description_prefix() -> None:
    """Empty string title is replaced by desc[:80] (line 143)."""
    desc = "Description that becomes the title when title is empty"
    raw = _one_finding(title="", description=desc)
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == desc[:80]


@pytest.mark.unit
def test_whitespace_title_falls_back_to_description_prefix() -> None:
    """Whitespace-only title is replaced by desc[:80] (line 143)."""
    desc = "Another description used as title fallback path"
    raw = _one_finding(title="   ", description=desc)
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == desc[:80]


@pytest.mark.unit
def test_long_description_title_fallback_is_truncated_at_80() -> None:
    """Title fallback is capped at 80 chars even when description is longer."""
    desc = "X" * 120
    raw = _one_finding(title="", description=desc)
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == desc[:80]
    assert len(result.findings[0].title) == 80


# ---------------------------------------------------------------------------
# _extract_json_array() — bracket extraction state machine
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bracket_extraction_from_prefixed_text() -> None:
    """Valid JSON array preceded by garbage text is extracted via bracket scan."""
    finding = {
        "title": "F",
        "description": "Some description",
        "category": "style",
        "severity": "nit",
    }
    raw = f"SOME GARBAGE PREFIX {json.dumps([finding])}"
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert result.findings[0].title == "F"


@pytest.mark.unit
def test_bracket_extraction_bracket_inside_string_does_not_confuse_depth() -> None:
    """Bracket character inside a JSON string value is ignored by the depth counter."""
    finding = {
        "title": "Has ] bracket in value",
        "description": "The character ] appears inside this description string",
        "category": "style",
        "severity": "nit",
    }
    # Prepend garbage so json.loads() fails and bracket extraction is needed.
    raw = f"garbage: {json.dumps([finding])}"
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1
    assert "]" in result.findings[0].title


@pytest.mark.unit
def test_bracket_extraction_with_escaped_quote_in_string() -> None:
    """Escaped quotes inside JSON strings are handled correctly by escape_next flag."""
    finding = {
        "title": 'Has \\"escaped\\" quote',
        "description": 'Description with \\"escaped\\" content for branch coverage',
        "category": "style",
        "severity": "nit",
    }
    raw_json = json.dumps([finding])
    # Force bracket extraction by prepending garbage.
    raw = f"preamble text: {raw_json}"
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1


@pytest.mark.unit
def test_bracket_found_but_content_invalid_json_returns_format_failure() -> None:
    """Bracket extracted but interior is not valid JSON → FORMAT_FAILURE (lines 129-130)."""
    # "prefix [not valid json at all]" — bracket found, depth returns to 0,
    # but json.loads of the bracketed content fails → return None → FORMAT_FAILURE.
    raw = "prefix [not valid json at all]"
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.FORMAT_FAILURE


@pytest.mark.unit
def test_unclosed_bracket_returns_format_failure() -> None:
    """An opening bracket with no matching close → depth never 0 → return None (line 131)."""
    raw = '[{"unclosed": "bracket", "description": "never closed"'
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.FORMAT_FAILURE


@pytest.mark.unit
def test_no_bracket_in_invalid_json_returns_format_failure() -> None:
    """JSONDecodeError with no '[' in text → bracket start=-1 → return None (lines 103-105)."""
    raw = "completely invalid, no brackets whatsoever"
    result = parse_model_response(raw, source_model=_MODEL)
    assert result.status == EnumParseStatus.FORMAT_FAILURE
