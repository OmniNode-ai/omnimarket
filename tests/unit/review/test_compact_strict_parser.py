# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""compact_strict mode of node_review_response_parser_compute (OMN-20910, plan ticket 14).

Fail closed: a verdict cut mid-array, schema-invalid, ``done`` absent,
``more: true``, an ``X`` finding with no family, an unknown ``R:`` id, a
provider stop reason of ``max_tokens`` or an unknown catalog code each give a
failed result with no findings. A mandatory ``R:`` finding comes out critical.
Frozen v1 replies still pass unchanged in the default tolerant mode.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_compact_strict_parse import (
    FAMILY_CATEGORY,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.models.model_compact_verdict import (
    FAMILIES,
)
from omnimarket.review.node_io import (
    EnumReviewParseMode,
    ModelReviewResponseParserRequest,
)
from omnimarket.review.response_parser import (
    EnumParseFailureKind,
    EnumParseStatus,
    ModelParseResult,
)

_KNOWN_CODES = {"T01": "testing", "S02": "security"}
_RULES = {"no-hardcoded-topics": False, "never-skip-hooks": True}


def _verdict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": "1.0.0",
        "f": [{"c": "T01", "p": "src/x.py", "l": 42, "s": "M"}],
        "more": False,
        "done": True,
    }
    base.update(overrides)
    return base


async def _parse(
    raw: str,
    *,
    stop_reason: str | None = None,
    known_codes: dict[str, str] | None = None,
    standing_rules: dict[str, bool] | None = None,
    max_findings: int = 25,
) -> ModelParseResult:
    output = await HandlerResponseParserCompute().handle(
        ModelReviewResponseParserRequest(
            correlation_id=uuid4(),
            raw_text=raw,
            source_model="sonnet",
            mode=EnumReviewParseMode.COMPACT_STRICT,
            stop_reason=stop_reason,
            known_codes=_KNOWN_CODES if known_codes is None else known_codes,
            standing_rules=_RULES if standing_rules is None else standing_rules,
            max_findings=max_findings,
        )
    )
    assert isinstance(output.result, ModelParseResult)
    return output.result


def _assert_failed(
    result: ModelParseResult, kind: EnumParseFailureKind | None = None
) -> None:
    assert result.status != EnumParseStatus.SUCCESS
    assert result.findings == []
    assert result.error_message
    if kind is not None:
        assert result.failure_kind == kind


@pytest.mark.unit
def test_every_family_maps_to_a_category() -> None:
    assert set(FAMILY_CATEGORY) == set(FAMILIES)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_verdict_parses_to_findings() -> None:
    result = await _parse(json.dumps(_verdict()), stop_reason="tool_use")
    assert result.status == EnumParseStatus.SUCCESS
    assert result.failure_kind is None
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.defect_code == "T01"
    assert finding.category == EnumFindingCategory.SCOPE_VIOLATION  # testing family
    assert finding.severity == EnumFindingSeverity.MAJOR
    assert finding.evidence.file_path == "src/x.py"
    assert finding.evidence.line_range == {"start": 42, "end": 42}
    assert finding.source_model == "sonnet"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_verdict_has_no_findings_and_succeeds() -> None:
    result = await _parse(json.dumps(_verdict(f=[])))
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_level_finding_has_no_line_range() -> None:
    raw = json.dumps(_verdict(f=[{"c": "S02", "p": "a.py", "l": 0, "s": "C"}]))
    result = await _parse(raw)
    assert result.findings[0].evidence.line_range is None
    assert result.findings[0].category == EnumFindingCategory.SECURITY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cut_mid_array_fails_closed() -> None:
    full = json.dumps(
        _verdict(
            f=[
                {"c": "T01", "p": "a.py", "l": 1, "s": "M"},
                {"c": "S02", "p": "b.py", "l": 2, "s": "C"},
            ]
        )
    )
    cut = full[: full.index('"b.py"')]
    result = await _parse(cut)
    _assert_failed(result, EnumParseFailureKind.TRUNCATED)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cut_before_done_with_complete_findings_fails_closed() -> None:
    raw = '{"v":"1.0.0","f":[{"c":"T01","p":"a.py","l":1,"s":"M"}],"more":false'
    _assert_failed(await _parse(raw), EnumParseFailureKind.TRUNCATED)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["max_tokens", "length", "refusal"])
async def test_abnormal_stop_reason_fails_closed_even_when_json_is_complete(
    stop_reason: str,
) -> None:
    result = await _parse(json.dumps(_verdict()), stop_reason=stop_reason)
    _assert_failed(result, EnumParseFailureKind.TRUNCATED)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["tool_use", "end_turn", "stop"])
async def test_normal_stop_reasons_pass(stop_reason: str) -> None:
    result = await _parse(json.dumps(_verdict()), stop_reason=stop_reason)
    assert result.status == EnumParseStatus.SUCCESS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_done_absent_fails_closed() -> None:
    body = _verdict()
    del body["done"]
    _assert_failed(await _parse(json.dumps(body)), EnumParseFailureKind.SCHEMA_INVALID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_done_false_fails_closed() -> None:
    _assert_failed(
        await _parse(json.dumps(_verdict(done=False))),
        EnumParseFailureKind.SCHEMA_INVALID,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_more_true_is_overflow_with_no_findings() -> None:
    result = await _parse(json.dumps(_verdict(more=True)))
    _assert_failed(result, EnumParseFailureKind.OVERFLOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_schema_invalid_findings_fail_closed() -> None:
    bad_findings: list[dict[str, Any]] = [
        {"c": "T01", "p": "a.py", "l": "42", "s": "M"},  # line is a string
        {"c": "T01", "p": "a.py", "l": -1, "s": "M"},  # negative line
        {"c": "T01", "p": "a.py", "l": 1, "s": "critical"},  # not C/M/m/n
        {"c": "T01", "p": "", "l": 1, "s": "M"},  # empty path
        {"c": "T01", "p": "a.py", "l": 1},  # severity absent
        {"c": "T01", "p": "a.py", "l": True, "s": "M"},  # bool is not an int
        {"c": "T01", "p": "a.py", "l": 1, "s": "M", "extra": "x"},  # extra field
    ]
    for item in bad_findings:
        result = await _parse(json.dumps(_verdict(f=[item])))
        _assert_failed(result, EnumParseFailureKind.SCHEMA_INVALID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_object_and_prose_fail_closed() -> None:
    _assert_failed(await _parse("[]"), EnumParseFailureKind.SCHEMA_INVALID)
    _assert_failed(
        await _parse("looks fine to me"), EnumParseFailureKind.SCHEMA_INVALID
    )
    _assert_failed(await _parse(""), EnumParseFailureKind.SCHEMA_INVALID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_salvage_of_fenced_or_wrapped_json() -> None:
    fenced = "```json\n" + json.dumps(_verdict()) + "\n```"
    _assert_failed(await _parse(fenced))
    wrapped = "Here is the review: " + json.dumps(_verdict())
    _assert_failed(await _parse(wrapped))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_more_findings_than_max_fails_closed() -> None:
    items = [{"c": "T01", "p": "a.py", "l": i, "s": "m"} for i in range(3)]
    result = await _parse(json.dumps(_verdict(f=items)), max_findings=2)
    _assert_failed(result, EnumParseFailureKind.SCHEMA_INVALID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_catalog_code_fails_closed() -> None:
    raw = json.dumps(_verdict(f=[{"c": "Z99", "p": "a.py", "l": 1, "s": "M"}]))
    _assert_failed(await _parse(raw), EnumParseFailureKind.SCHEMA_INVALID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_catalog_means_only_x_and_r_codes_can_pass() -> None:
    raw = json.dumps(_verdict())  # T01 is not in an empty catalog
    _assert_failed(await _parse(raw, known_codes={}))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_catalog_code_must_not_carry_a_family() -> None:
    item = {"c": "T01", "p": "a.py", "l": 1, "s": "M", "y": "testing"}
    _assert_failed(
        await _parse(json.dumps(_verdict(f=[item]))),
        EnumParseFailureKind.SCHEMA_INVALID,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_x_finding_without_family_fails_closed() -> None:
    item = {"c": "X", "p": "a.py", "l": 1, "s": "M", "n": "odd retry loop"}
    _assert_failed(
        await _parse(json.dumps(_verdict(f=[item]))),
        EnumParseFailureKind.SCHEMA_INVALID,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_x_finding_with_unknown_family_fails_closed() -> None:
    item = {"c": "X", "p": "a.py", "l": 1, "s": "M", "y": "vibes", "n": "odd"}
    _assert_failed(await _parse(json.dumps(_verdict(f=[item]))))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_x_finding_without_note_fails_closed() -> None:
    item = {"c": "X", "p": "a.py", "l": 1, "s": "M", "y": "correctness"}
    _assert_failed(await _parse(json.dumps(_verdict(f=[item]))))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_note_over_120_characters_fails_closed() -> None:
    item = {"c": "X", "p": "a.py", "l": 1, "s": "M", "y": "style", "n": "n" * 121}
    _assert_failed(await _parse(json.dumps(_verdict(f=[item]))))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_x_finding_with_family_and_note_passes() -> None:
    item = {
        "c": "X",
        "p": "a.py",
        "l": 7,
        "s": "m",
        "y": "performance",
        "n": "unbounded retry loop",
    }
    result = await _parse(json.dumps(_verdict(f=[item])))
    assert result.status == EnumParseStatus.SUCCESS
    finding = result.findings[0]
    assert finding.defect_code == "X"
    assert finding.category == EnumFindingCategory.LOGIC_ERROR
    assert "unbounded retry loop" in finding.description


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_standing_rule_id_fails_closed() -> None:
    item = {"c": "R:made-up-rule", "p": "a.py", "l": 1, "s": "M", "y": "style"}
    _assert_failed(
        await _parse(json.dumps(_verdict(f=[item]))),
        EnumParseFailureKind.SCHEMA_INVALID,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_finding_without_family_fails_closed() -> None:
    item = {"c": "R:no-hardcoded-topics", "p": "a.py", "l": 1, "s": "M"}
    _assert_failed(await _parse(json.dumps(_verdict(f=[item]))))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mandatory_r_finding_comes_out_critical() -> None:
    item = {"c": "R:never-skip-hooks", "p": "a.py", "l": 3, "s": "n", "y": "style"}
    result = await _parse(json.dumps(_verdict(f=[item])))
    assert result.status == EnumParseStatus.SUCCESS
    assert result.findings[0].severity == EnumFindingSeverity.CRITICAL
    assert result.findings[0].defect_code == "R:never-skip-hooks"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_mandatory_r_finding_keeps_its_severity() -> None:
    item = {"c": "R:no-hardcoded-topics", "p": "a.py", "l": 3, "s": "m", "y": "style"}
    result = await _parse(json.dumps(_verdict(f=[item])))
    assert result.findings[0].severity == EnumFindingSeverity.MINOR


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_bad_finding_discards_the_good_ones() -> None:
    good = {"c": "T01", "p": "a.py", "l": 1, "s": "M"}
    bad = {"c": "Z99", "p": "b.py", "l": 1, "s": "M"}
    result = await _parse(json.dumps(_verdict(f=[good, bad])))
    _assert_failed(result)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_message_never_echoes_reply_text() -> None:
    secret = "SECRET-TOKEN-IN-REPLY"
    item = {"c": "X", "p": secret, "l": 1, "s": "M", "n": secret}
    result = await _parse(json.dumps(_verdict(f=[item])))
    _assert_failed(result)
    assert secret not in result.error_message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frozen_v1_reply_still_passes_in_default_mode() -> None:
    raw = json.dumps(
        [
            {
                "category": "security",
                "severity": "major",
                "title": "XSS",
                "description": "Unescaped output",
            }
        ]
    )
    output = await HandlerResponseParserCompute().handle(
        ModelReviewResponseParserRequest(
            correlation_id=uuid4(), raw_text=raw, source_model="qwen3-coder"
        )
    )
    assert isinstance(output.result, ModelParseResult)
    assert output.result.status == EnumParseStatus.SUCCESS
    assert output.result.failure_kind is None
    assert len(output.result.findings) == 1
    assert output.result.findings[0].defect_code is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_v1_reply_is_rejected_by_compact_strict() -> None:
    raw = json.dumps(
        [{"category": "security", "severity": "major", "description": "x"}]
    )
    _assert_failed(await _parse(raw))
