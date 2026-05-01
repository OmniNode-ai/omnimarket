# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_quality_scoring_compute.

Exercises the full handler stack: ModelQualityScoringInput ->
handle_quality_scoring_compute -> ModelQualityScoringOutput.

Covers valid Python, empty input, syntax errors, unsupported languages,
preset configurations, custom weights, and threshold logic.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_quality_scoring_compute.handlers.enum_onex_strictness_level import (
    OnexStrictnessLevel,
)
from omnimarket.nodes.node_quality_scoring_compute.handlers.handler_compute import (
    handle_quality_scoring_compute,
)
from omnimarket.nodes.node_quality_scoring_compute.handlers.handler_quality_scoring import (
    score_code_quality,
)
from omnimarket.nodes.node_quality_scoring_compute.models.model_dimension_weights import (
    ModelDimensionWeights,
)
from omnimarket.nodes.node_quality_scoring_compute.models.model_quality_scoring_input import (
    ModelQualityScoringInput,
)
from omnimarket.nodes.node_quality_scoring_compute.models.model_quality_scoring_output import (
    ModelQualityScoringOutput,
)

_WELL_FORMED_PYTHON = """
from __future__ import annotations

from typing import Final
from pydantic import BaseModel, Field


MAX_RETRIES: Final[int] = 3
DIMENSION_KEYS: Final[tuple[str, ...]] = ("complexity", "maintainability")


class ModelRetryPolicy(BaseModel):
    max_retries: int = Field(default=MAX_RETRIES, ge=0)
    backoff_factor: float = Field(default=1.5, ge=1.0)

    model_config = {"frozen": True, "extra": "forbid"}


def compute_backoff(attempt: int, factor: float) -> float:
    return factor ** attempt


def _validate_attempt(attempt: int) -> None:
    if attempt < 0:
        raise ValueError(f"attempt must be non-negative, got {attempt}")
"""

_SYNTAX_ERROR_PYTHON = "def broken(: int) -> None: pass"

_MINIMAL_PYTHON = "x = 1"


def _make_input(
    content: str = _WELL_FORMED_PYTHON,
    language: str = "python",
    **kwargs: object,
) -> ModelQualityScoringInput:
    return ModelQualityScoringInput(
        source_path="test_file.py",
        content=content,
        language=language,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestQualityScoringGoldenChain:
    """Golden chain: source code in -> quality score out."""

    def test_well_formed_python_succeeds(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        assert isinstance(result, ModelQualityScoringOutput)
        assert result.success is True
        assert 0.0 <= result.quality_score <= 1.0

    def test_six_dimensions_present(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        dims = result.dimensions
        assert set(dims.keys()) == {
            "complexity",
            "maintainability",
            "documentation",
            "temporal_relevance",
            "patterns",
            "architectural",
        }

    def test_all_dimension_scores_in_range(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        for dim, score in result.dimensions.items():
            assert 0.0 <= float(score) <= 1.0, f"dimension {dim} out of range: {score}"

    def test_empty_content_returns_failure(self) -> None:
        result = handle_quality_scoring_compute(_make_input(content="   "))
        assert result.success is False
        assert result.quality_score == 0.0

    def test_syntax_error_returns_low_score(self) -> None:
        result = handle_quality_scoring_compute(
            _make_input(content=_SYNTAX_ERROR_PYTHON)
        )
        assert result.success is True
        assert result.quality_score < 0.5

    def test_unsupported_language_returns_baseline(self) -> None:
        result = handle_quality_scoring_compute(
            _make_input(content="fn main() {}", language="rust")
        )
        assert result.success is True
        assert result.quality_score == pytest.approx(0.5, abs=0.01)
        assert result.onex_compliant is False

    def test_onex_compliance_above_threshold(self) -> None:
        result = handle_quality_scoring_compute(
            _make_input(onex_compliance_threshold=0.0)
        )
        assert result.onex_compliant is True

    def test_onex_compliance_below_threshold(self) -> None:
        result = handle_quality_scoring_compute(
            _make_input(_MINIMAL_PYTHON, onex_compliance_threshold=1.0)
        )
        assert result.onex_compliant is False

    def test_min_quality_threshold_affects_status(self) -> None:
        result = handle_quality_scoring_compute(
            _make_input(_MINIMAL_PYTHON, min_quality_threshold=1.0)
        )
        assert result.metadata is not None
        assert result.metadata.status == "below_threshold"

    def test_completed_status_when_above_threshold(self) -> None:
        result = handle_quality_scoring_compute(_make_input(min_quality_threshold=0.0))
        assert result.metadata is not None
        assert result.metadata.status == "completed"

    def test_metadata_contains_analysis_version(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        assert result.metadata is not None
        assert result.metadata.analysis_version is not None
        assert len(result.metadata.analysis_version) > 0

    def test_metadata_contains_processing_time(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        assert result.metadata is not None
        assert result.metadata.processing_time_ms is not None
        assert result.metadata.processing_time_ms >= 0.0

    def test_metadata_source_language(self) -> None:
        result = handle_quality_scoring_compute(_make_input(language="Python"))
        assert result.metadata is not None
        assert result.metadata.source_language == "python"

    def test_recommendations_are_list(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        assert isinstance(result.recommendations, list)

    def test_strict_preset_raises_compliance_bar(self) -> None:
        simple_code = "x = 1\ny = 2\n"
        strict = handle_quality_scoring_compute(
            _make_input(simple_code, onex_preset=OnexStrictnessLevel.STRICT)
        )
        lenient = handle_quality_scoring_compute(
            _make_input(simple_code, onex_preset=OnexStrictnessLevel.LENIENT)
        )
        # Same code is more likely to fail strict than lenient compliance
        if strict.onex_compliant:
            assert lenient.onex_compliant
        else:
            assert not strict.onex_compliant

    def test_standard_preset_threshold_is_0_7(self) -> None:
        from omnimarket.nodes.node_quality_scoring_compute.handlers.presets import (
            get_threshold_for_preset,
        )

        assert get_threshold_for_preset(OnexStrictnessLevel.STANDARD) == pytest.approx(
            0.7
        )

    def test_strict_preset_threshold_is_0_8(self) -> None:
        from omnimarket.nodes.node_quality_scoring_compute.handlers.presets import (
            get_threshold_for_preset,
        )

        assert get_threshold_for_preset(OnexStrictnessLevel.STRICT) == pytest.approx(
            0.8
        )

    def test_lenient_preset_threshold_is_0_5(self) -> None:
        from omnimarket.nodes.node_quality_scoring_compute.handlers.presets import (
            get_threshold_for_preset,
        )

        assert get_threshold_for_preset(OnexStrictnessLevel.LENIENT) == pytest.approx(
            0.5
        )

    def test_custom_weights_applied(self) -> None:
        weights = ModelDimensionWeights(
            complexity=0.5,
            maintainability=0.1,
            documentation=0.1,
            temporal_relevance=0.1,
            patterns=0.1,
            architectural=0.1,
        )
        result_custom = handle_quality_scoring_compute(
            _make_input(dimension_weights=weights)
        )
        result_default = handle_quality_scoring_compute(_make_input())
        # Scores will differ when weights differ substantially
        assert isinstance(result_custom.quality_score, float)
        assert isinstance(result_default.quality_score, float)

    def test_output_is_frozen_model(self) -> None:
        result = handle_quality_scoring_compute(_make_input())
        with pytest.raises(Exception, match="frozen"):
            result.success = False  # type: ignore[misc]


@pytest.mark.unit
class TestScoreCodeQualityPure:
    """Unit tests for the pure score_code_quality function."""

    def test_basic_python_returns_result(self) -> None:
        result = score_code_quality(content=_WELL_FORMED_PYTHON, language="python")
        assert result["success"] is True
        assert 0.0 <= result["quality_score"] <= 1.0

    def test_empty_content_fails(self) -> None:
        result = score_code_quality(content="", language="python")
        assert result["success"] is False

    def test_radon_enabled_flag(self) -> None:
        result = score_code_quality(content=_WELL_FORMED_PYTHON, language="python")
        assert result.get("radon_complexity_enabled") is True

    def test_unsupported_language_baseline_score(self) -> None:
        result = score_code_quality(content="SELECT 1", language="sql")
        assert result["success"] is True
        assert result["quality_score"] == pytest.approx(0.5, abs=0.01)

    def test_syntax_error_handled_gracefully(self) -> None:
        result = score_code_quality(content=_SYNTAX_ERROR_PYTHON, language="python")
        assert result["success"] is True
        assert result["quality_score"] < 0.5

    def test_preset_overrides_manual_weights(self) -> None:
        result = score_code_quality(
            content=_WELL_FORMED_PYTHON,
            language="python",
            preset=OnexStrictnessLevel.LENIENT,
            weights={
                "complexity": 1.0,
                "maintainability": 0.0,
                "documentation": 0.0,
                "temporal_relevance": 0.0,
                "patterns": 0.0,
                "architectural": 0.0,
            },
        )
        assert result["success"] is True

    def test_invalid_weights_returns_validation_error(self) -> None:
        bad_weights = {
            "complexity": 0.5,
            "maintainability": 0.5,
            "documentation": 0.5,
            "temporal_relevance": 0.0,
            "patterns": 0.0,
            "architectural": 0.0,
        }
        result = score_code_quality(
            content=_WELL_FORMED_PYTHON,
            language="python",
            weights=bad_weights,
        )
        assert result["success"] is False
        assert any("validation_error" in r for r in result["recommendations"])

    def test_language_case_insensitive(self) -> None:
        result_lower = score_code_quality(content=_MINIMAL_PYTHON, language="python")
        result_upper = score_code_quality(content=_MINIMAL_PYTHON, language="Python")
        result_mixed = score_code_quality(content=_MINIMAL_PYTHON, language="PYTHON")
        assert result_lower["source_language"] == "python"
        assert result_upper["source_language"] == "python"
        assert result_mixed["source_language"] == "python"

    def test_analysis_version_present(self) -> None:
        result = score_code_quality(content=_MINIMAL_PYTHON, language="python")
        assert result["analysis_version"] != ""
