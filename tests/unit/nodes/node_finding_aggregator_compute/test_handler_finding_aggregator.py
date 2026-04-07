# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerFindingAggregator.

Related:
    - OMN-7795: Finding Aggregator COMPUTE node
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_finding_aggregator_compute.handlers.handler_finding_aggregator import (
    HandlerFindingAggregator,
    _jaccard_similarity,
    _tokenize,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_config import (
    ModelFindingAggregatorConfig,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_input import (
    ModelFindingAggregatorInput,
    ModelSourceFindings,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_output import (
    EnumAggregatedVerdict,
)


def _make_finding(
    rule_id: str = "ruff:E501",
    file_path: str = "src/main.py",
    line_start: int = 10,
    severity: str = "warning",
    normalized_message: str = "line too long",
    **extra: object,
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "file_path": file_path,
        "line_start": line_start,
        "severity": severity,
        "normalized_message": normalized_message,
        **extra,
    }


class TestJaccardSimilarity:
    def test_identical_sets(self) -> None:
        tokens = {"foo", "bar", "baz"}
        assert _jaccard_similarity(tokens, tokens) == 1.0

    def test_disjoint_sets(self) -> None:
        assert _jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self) -> None:
        a = {"line", "too", "long"}
        b = {"line", "too", "short"}
        # intersection = {line, too} = 2, union = {line, too, long, short} = 4
        assert _jaccard_similarity(a, b) == pytest.approx(0.5)

    def test_empty_sets(self) -> None:
        assert _jaccard_similarity(set(), set()) == 1.0

    def test_one_empty(self) -> None:
        assert _jaccard_similarity({"a"}, set()) == 0.0


class TestTokenize:
    def test_basic(self) -> None:
        assert _tokenize("Line too long") == {"line", "too", "long"}

    def test_empty(self) -> None:
        assert _tokenize("") == set()


@pytest.mark.asyncio
class TestHandlerFindingAggregator:
    async def test_single_source_no_dedup(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="deepseek-r1",
                    findings=(
                        _make_finding(rule_id="ruff:E501", file_path="a.py"),
                        _make_finding(rule_id="mypy:return", file_path="b.py"),
                    ),
                ),
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.correlation_id == cid
        assert result.total_input_findings == 2
        assert result.total_merged_findings == 2
        assert result.total_duplicates_removed == 0
        assert result.source_model_count == 1
        assert result.verdict == EnumAggregatedVerdict.RISKS_NOTED

    async def test_duplicate_findings_merged(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        finding = _make_finding()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(model_name="deepseek-r1", findings=(finding,)),
                ModelSourceFindings(model_name="qwen3-coder", findings=(finding,)),
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_input_findings == 2
        assert result.total_merged_findings == 1
        assert result.total_duplicates_removed == 1
        merged = result.merged_findings[0]
        assert set(merged.source_models) == {"deepseek-r1", "qwen3-coder"}
        assert merged.merged_count == 2

    async def test_different_findings_not_merged(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="deepseek-r1",
                    findings=(_make_finding(rule_id="ruff:E501", file_path="a.py"),),
                ),
                ModelSourceFindings(
                    model_name="qwen3-coder",
                    findings=(
                        _make_finding(
                            rule_id="mypy:return",
                            file_path="b.py",
                            normalized_message="missing return type",
                        ),
                    ),
                ),
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_merged_findings == 2
        assert result.total_duplicates_removed == 0

    async def test_severity_promotion(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(_make_finding(severity="warning"),),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(_make_finding(severity="error"),),
                ),
            ),
            config=ModelFindingAggregatorConfig(severity_promotes_on_conflict=True),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_merged_findings == 1
        assert result.merged_findings[0].severity == "error"

    async def test_no_severity_promotion(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(_make_finding(severity="warning"),),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(_make_finding(severity="error"),),
                ),
            ),
            config=ModelFindingAggregatorConfig(severity_promotes_on_conflict=False),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_merged_findings == 1
        # First finding's severity is kept
        assert result.merged_findings[0].severity == "warning"

    async def test_verdict_clean_on_empty(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(ModelSourceFindings(model_name="model-a", findings=()),),
        )
        result = await handler.handle(cid, input_data)
        assert result.verdict == EnumAggregatedVerdict.CLEAN
        assert result.total_merged_findings == 0

    async def test_verdict_blocking_on_error(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(_make_finding(severity="error"),),
                ),
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.verdict == EnumAggregatedVerdict.BLOCKING_ISSUE

    async def test_custom_model_weights(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        finding = _make_finding()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(model_name="strong-model", findings=(finding,)),
                ModelSourceFindings(model_name="weak-model", findings=()),
            ),
            config=ModelFindingAggregatorConfig(
                model_weights={"strong-model": 0.8, "weak-model": 0.2},
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_merged_findings == 1
        assert result.merged_findings[0].weighted_score == pytest.approx(0.8)

    async def test_jaccard_threshold_low_merges_more(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        _make_finding(normalized_message="line too long in function"),
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(
                        _make_finding(normalized_message="line too long exceeds limit"),
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(jaccard_threshold=0.3),
        )
        result = await handler.handle(cid, input_data)
        # With low threshold, similar messages should merge
        assert result.total_merged_findings == 1

    async def test_jaccard_threshold_high_keeps_separate(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        _make_finding(normalized_message="line too long in function"),
                    ),
                ),
                ModelSourceFindings(
                    model_name="model-b",
                    findings=(
                        _make_finding(normalized_message="line too long exceeds limit"),
                    ),
                ),
            ),
            config=ModelFindingAggregatorConfig(jaccard_threshold=0.95),
        )
        result = await handler.handle(cid, input_data)
        # With high threshold, slightly different messages stay separate
        assert result.total_merged_findings == 2

    async def test_skips_findings_with_missing_fields(self) -> None:
        handler = HandlerFindingAggregator()
        cid = uuid4()
        input_data = ModelFindingAggregatorInput(
            correlation_id=cid,
            sources=(
                ModelSourceFindings(
                    model_name="model-a",
                    findings=(
                        {"rule_id": "ruff:E501"},  # Missing required fields
                        _make_finding(),
                    ),
                ),
            ),
        )
        result = await handler.handle(cid, input_data)
        assert result.total_input_findings == 2
        assert result.total_merged_findings == 1

    async def test_handler_properties(self) -> None:
        handler = HandlerFindingAggregator()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"
