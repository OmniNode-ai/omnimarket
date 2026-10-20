# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the retrospective prompt cost estimate (OMN-20388).

An uploaded export goes through node_prompt_cost_estimate_compute and every
figure is checked against the pricing manifest directly, never against a
number restated here.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable
from pydantic import ValidationError

from omnimarket.nodes.node_prompt_cost_estimate_compute import (
    NOT_DELEGATED,
    PRICING_SOURCE,
    EnumExportFormat,
    EnumModelSource,
    EnumUnpricedReason,
    ExportParseError,
    HandlerPromptCostEstimate,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_prompt_cost_estimate_compute"
)
_PER_1K = Decimal(1000)
_CODE_PROMPT = "write a python function that parses ISO dates and returns UTC"
_CODE_REPLY = "def parse(value):\n    return datetime.fromisoformat(value)\n" * 40
_RESEARCH_PROMPT = "what is the capital of france and why did it become the capital"
_RESEARCH_REPLY = "Paris became the capital because " + "of history " * 60


def _chatgpt(*turns: tuple[str, str, str]) -> str:
    mapping: dict[str, object] = {}
    for index, (prompt, reply, model) in enumerate(turns):
        created = 1_760_000_000 + index * 10
        mapping[f"u{index}"] = {
            "message": {
                "author": {"role": "user"},
                "create_time": created,
                "content": {"content_type": "text", "parts": [prompt]},
            }
        }
        mapping[f"a{index}"] = {
            "message": {
                "author": {"role": "assistant"},
                "create_time": created + 1,
                "metadata": {"model_slug": model},
                "content": {"content_type": "text", "parts": [reply]},
            }
        }
    return json.dumps([{"id": "conv-1", "mapping": mapping}])


def _estimate(
    content: str,
    export_format: EnumExportFormat = EnumExportFormat.CHATGPT_CONVERSATIONS,
    declared_model: str | None = None,
) -> ModelPromptCostEstimateResult:
    return HandlerPromptCostEstimate().handle(
        ModelPromptCostEstimateRequest(
            consent_given=True,
            export_format=export_format,
            content=content,
            declared_model=declared_model,
        )
    )


def _manifest_cost(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    entry = ModelPricingTable.from_yaml().get_entry(model)
    assert entry is not None, f"{model} is not in the pricing manifest"
    return (
        Decimal(str(entry.input_cost_per_1k)) * tokens_in
        + Decimal(str(entry.output_cost_per_1k)) * tokens_out
    ) / _PER_1K


# AC1 -----------------------------------------------------------------------


def test_two_exports_with_different_model_mixes_give_different_headlines() -> None:
    premium = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, "gpt-4o"),
        )
    )
    mixed = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o-mini"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, "gpt-4o"),
        )
    )
    assert premium.prompts_priced == mixed.prompts_priced == 2
    assert premium.actual_cost_usd != mixed.actual_cost_usd
    assert premium.saving_usd != mixed.saving_usd
    assert {group.task_type for group in premium.by_task_type} == {
        "code_generation",
        "research",
    }


def test_the_headline_is_the_sum_of_its_rows_and_task_types() -> None:
    result = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, "gpt-4o"),
        )
    )
    assert result.actual_cost_usd == sum(row.actual_cost_usd for row in result.rows)
    assert result.saving_usd == sum(group.saving_usd for group in result.by_task_type)
    assert result.actual_cost_usd - result.delegated_cost_usd == result.saving_usd


# AC2 -----------------------------------------------------------------------


def test_every_price_agrees_with_the_pricing_manifest() -> None:
    result = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, "o1"),
        )
    )
    assert result.rows
    for row in result.rows:
        assert row.actual_cost_usd == _manifest_cost(
            row.source_model, row.tokens_in, row.tokens_out
        )
        assert row.delegated_cost_usd == _manifest_cost(
            row.recommended_model, row.tokens_in, row.tokens_out
        )
    assert result.pricing_source == PRICING_SOURCE
    assert (
        result.pricing_manifest_version == ModelPricingTable.from_yaml().schema_version
    )


def test_the_estimator_source_holds_no_price_or_rate_literal() -> None:
    offenders: list[str] = []
    for path in sorted(_NODE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path.name}:{node.lineno} float {node.value!r}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Decimal"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value not in ("0", 0)
            ):
                offenders.append(f"{path.name}:{node.lineno} Decimal literal")
    assert offenders == []


# AC3 -----------------------------------------------------------------------


def test_a_model_the_manifest_lacks_is_unpriced_and_never_substituted() -> None:
    absent = "gpt-absent-from-manifest"
    assert ModelPricingTable.from_yaml().get_entry(absent) is None
    with_absent = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, absent),
        )
    )
    without = _estimate(_chatgpt((_CODE_PROMPT, _CODE_REPLY, "gpt-4o")))

    assert with_absent.prompts_total == 2
    assert with_absent.prompts_unpriced == 1
    assert [(g.model, g.reason, g.prompts) for g in with_absent.unpriced] == [
        (absent, EnumUnpricedReason.MODEL_NOT_IN_MANIFEST, 1)
    ]
    assert all(row.source_model != absent for row in with_absent.rows)
    assert with_absent.actual_cost_usd == without.actual_cost_usd
    assert with_absent.saving_usd == without.saving_usd


def test_a_prompt_naming_no_model_is_unpriced_unless_the_visitor_declares_one() -> None:
    export = json.dumps(
        [
            {
                "uuid": "conv-c",
                "chat_messages": [
                    {"sender": "human", "text": _CODE_PROMPT},
                    {"sender": "assistant", "text": _CODE_REPLY},
                ],
            }
        ]
    )
    undeclared = _estimate(export, EnumExportFormat.CLAUDE_CONVERSATIONS)
    assert undeclared.rows == ()
    assert [(g.model, g.reason) for g in undeclared.unpriced] == [
        ("", EnumUnpricedReason.NO_MODEL_NAMED)
    ]

    declared = _estimate(
        export, EnumExportFormat.CLAUDE_CONVERSATIONS, "claude-sonnet-4-6"
    )
    (row,) = declared.rows
    assert row.source_model == "claude-sonnet-4-6"
    assert row.model_source is EnumModelSource.DECLARED


# AC4 -----------------------------------------------------------------------


def test_every_row_carries_a_recommended_model_and_a_saving() -> None:
    result = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            (_RESEARCH_PROMPT, _RESEARCH_REPLY, "gpt-4o-mini"),
            ("review this PR for me", "Looks fine.", "gpt-4o"),
        )
    )
    assert len(result.rows) == 3
    for row in result.rows:
        assert row.recommended_model
        assert row.recommended_tier
        assert row.saving_usd is not None
        assert row.saving_usd >= 0


def test_delegation_is_recommended_only_when_it_is_cheaper() -> None:
    result = _estimate(
        _chatgpt(
            (_CODE_PROMPT, _CODE_REPLY, "gpt-4o"),
            ("review this PR for me", "Looks fine.", "gpt-4o"),
        )
    )
    code, review = result.rows
    assert code.task_type == "code_generation"
    assert code.recommended_tier != NOT_DELEGATED
    assert code.saving_usd > 0
    assert review.recommended_tier == NOT_DELEGATED
    assert review.recommended_model == review.source_model
    assert review.saving_usd == 0


# AC5 -----------------------------------------------------------------------


def test_the_result_is_labelled_estimated_and_names_its_pricing_source() -> None:
    result = _estimate(_chatgpt((_CODE_PROMPT, _CODE_REPLY, "gpt-4o")))
    assert result.basis == "estimated"
    assert result.pricing_source == "omnibase_infra pricing_manifest.yaml"
    assert all(row.basis == "estimated" for row in result.rows)


def test_token_counts_read_from_text_are_marked_estimated_and_recorded_ones_are_not() -> (
    None
):
    from_text = _estimate(_chatgpt((_CODE_PROMPT, _CODE_REPLY, "gpt-4o")))
    assert from_text.prompts_tokens_estimated == 1
    assert from_text.rows[0].tokens_estimated is True

    transcript = "\n".join(
        json.dumps(entry)
        for entry in (
            {
                "type": "user",
                "uuid": "p1",
                "sessionId": "s1",
                "timestamp": "2026-10-01T10:00:00Z",
                "message": {"role": "user", "content": _CODE_PROMPT},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": "s1",
                "timestamp": "2026-10-01T10:00:05Z",
                "message": {
                    "id": "msg-1",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 1200, "output_tokens": 800},
                },
            },
        )
    )
    recorded = _estimate(transcript, EnumExportFormat.CLAUDE_CODE_JSONL)
    (row,) = recorded.rows
    assert row.tokens_estimated is False
    assert (row.tokens_in, row.tokens_out) == (1200, 800)
    assert row.prompt_text == _CODE_PROMPT
    assert recorded.prompts_tokens_estimated == 0


def test_a_csv_with_counts_is_measured_and_one_without_is_estimated() -> None:
    measured = _estimate(
        f'prompt,model,input_tokens,output_tokens\n"{_CODE_PROMPT}",gpt-4o,900,400\n',
        EnumExportFormat.CSV,
    )
    assert measured.rows[0].tokens_estimated is False
    estimated = _estimate(
        f'prompt,model\n"{_CODE_PROMPT}",gpt-4o\n', EnumExportFormat.CSV
    )
    assert estimated.rows[0].tokens_estimated is True


# AC6 -----------------------------------------------------------------------


def test_an_estimate_without_consent_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPromptCostEstimateRequest.model_validate(
            {
                "consent_given": False,
                "export_format": "csv",
                "content": "prompt\nhello\n",
            }
        )


def test_an_upload_that_is_not_its_declared_shape_is_refused() -> None:
    with pytest.raises(ExportParseError):
        _estimate("not json", EnumExportFormat.CHATGPT_CONVERSATIONS)
    with pytest.raises(ExportParseError):
        _estimate("model,tokens\nx,1\n", EnumExportFormat.CSV)
