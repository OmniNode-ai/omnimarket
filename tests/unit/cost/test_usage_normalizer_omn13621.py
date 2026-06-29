# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13621: parity tests for the canonical omnimarket usage normalizer.

These mirror the SEA ``tests/unit/test_usage_normalizer.py`` mapping suite that
proved the routed normalization behavior. The canonical home is
``omnimarket.cost.usage_normalizer``; the SEA module is deleted after this parity
is proven (no shim).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.cost.usage_normalizer import ModelUsageResult, normalize_usage
from omnimarket.enums.enum_usage_source import EnumUsageSource


def test_openai_compat_returns_measured() -> None:
    data = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    }
    result = normalize_usage(
        data, provider="openai", prompt_text="hello", response_text="world"
    )
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.total_tokens == 150
    assert result.provider_format == "openai_compat"
    assert result.estimation_method is None
    assert result.fallback_reason is None


def test_gemini_native_returns_measured() -> None:
    data = {"usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 75}}
    result = normalize_usage(
        data, provider="gemini", prompt_text="hello", response_text="world"
    )
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.input_tokens == 200
    assert result.output_tokens == 75
    assert result.total_tokens == 275
    assert result.provider_format == "gemini_native"
    assert result.estimation_method is None
    assert result.fallback_reason is None


def test_missing_usage_with_text_returns_estimated() -> None:
    data = {"choices": [{"message": {"content": "some response"}}]}
    result = normalize_usage(
        data,
        provider="unknown",
        prompt_text="test prompt text here",
        response_text="response text",
    )
    assert result.usage_source == EnumUsageSource.ESTIMATED
    assert result.estimation_method == "char_length_div_4"
    assert result.fallback_reason == "provider returned no usage data"
    assert result.provider_format == "unknown"
    assert result.input_tokens == len("test prompt text here") // 4
    assert result.output_tokens == len("response text") // 4


def test_empty_response_returns_unknown() -> None:
    result = normalize_usage({}, provider="test")
    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_missing_usage_no_text_returns_unknown() -> None:
    data = {"choices": [{"message": {"content": "something"}}]}
    result = normalize_usage(data, provider="test")
    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.fallback_reason == "provider returned no usage data"


def test_estimation_method_set_only_on_estimated() -> None:
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    result = normalize_usage(data, provider="openai")
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.estimation_method is None


def test_usage_source_never_measured_when_estimating() -> None:
    data: dict[str, object] = {}
    result = normalize_usage(
        data, provider="test", prompt_text="abc", response_text="def"
    )
    assert result.usage_source != EnumUsageSource.MEASURED


def test_openai_compat_partial_missing_fields_falls_back() -> None:
    data = {"usage": {"prompt_tokens": 50}}
    result = normalize_usage(
        data, provider="openai", prompt_text="hello world", response_text="reply"
    )
    assert result.usage_source in (EnumUsageSource.ESTIMATED, EnumUsageSource.UNKNOWN)


def test_gemini_zero_tokens_not_treated_as_missing() -> None:
    data = {"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0}}
    result = normalize_usage(data, provider="gemini")
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_model_usage_result_is_frozen() -> None:
    result = ModelUsageResult(
        input_tokens=5, output_tokens=3, usage_source=EnumUsageSource.MEASURED
    )
    with pytest.raises(ValidationError):
        result.input_tokens = 10  # type: ignore[misc]


def test_total_tokens_computed_correctly() -> None:
    data = {"usage": {"prompt_tokens": 40, "completion_tokens": 60}}
    result = normalize_usage(data, provider="openai")
    assert result.total_tokens == 100


def test_negative_prompt_tokens_falls_back() -> None:
    data = {"usage": {"prompt_tokens": -5, "completion_tokens": 5}}
    result = normalize_usage(data, provider="openai")
    assert result.usage_source in (EnumUsageSource.ESTIMATED, EnumUsageSource.UNKNOWN)


def test_bool_prompt_tokens_rejected() -> None:
    data = {"usage": {"prompt_tokens": True, "completion_tokens": 5}}
    result = normalize_usage(data, provider="openai")
    assert result.usage_source in (EnumUsageSource.ESTIMATED, EnumUsageSource.UNKNOWN)
