# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-21189): the inference result carries the served model, settings and
usage; a field the provider did not report reads ``unknown``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
    ModelInferenceJsonObjectResponseFormat,
)
from omnimarket.inference.protocol_config import ModelInferenceProtocolSelection
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.models.model_inference_provenance import (
    UNKNOWN,
    ModelInferenceProvenance,
    ModelServedInferenceResult,
    ModelServedSettings,
    ModelServedUsage,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_delegation_review_inference import (
    HandlerDelegationReviewInference,
)

_ROUTE = "review_primary"


def _bridge() -> AdapterInferenceBridge:
    return AdapterInferenceBridge(
        ModelInferenceBridgeConfig(
            model_configs={
                _ROUTE: {
                    "transport": "http",
                    "base_url": "http://localhost:8000",
                    "model_id": "requested-model",
                }
            }
        )
    )


def _completion(body: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200, json=body, request=httpx.Request("POST", "http://localhost:8000")
    )


async def _infer_with(
    body: dict[str, object], temperature: float | None = 0.2
) -> ModelServedInferenceResult:
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as post:
        post.return_value = _completion(body)
        return await _bridge().infer_with_provenance(
            model_key=_ROUTE,
            system_prompt="s",
            user_prompt="u",
            timeout_seconds=5.0,
            temperature=temperature,
        )


@pytest.mark.asyncio
async def test_complete_fixture_carries_served_model_settings_and_usage() -> None:
    result = await _infer_with(
        {
            "model": "served-model-2026-10",
            "choices": [{"message": {"content": "[]"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
            },
        },
        temperature=0.4,
    )

    assert result.text == "[]"
    assert result.provenance == ModelInferenceProvenance(
        served_model="served-model-2026-10",
        settings=ModelServedSettings(temperature=0.4, max_tokens=2048),
        usage=ModelServedUsage(input_tokens=120, output_tokens=30, total_tokens=150),
    )


@pytest.mark.asyncio
async def test_missing_usage_and_model_read_unknown_not_the_requested_route() -> None:
    result = await _infer_with({"choices": [{"message": {"content": "[]"}}]})

    assert result.text == "[]"
    assert result.provenance.served_model == UNKNOWN
    assert result.provenance.usage == ModelServedUsage(
        input_tokens=UNKNOWN, output_tokens=UNKNOWN, total_tokens=UNKNOWN
    )


@pytest.mark.asyncio
async def test_partial_usage_reads_unknown_only_for_the_missing_field() -> None:
    result = await _infer_with(
        {
            "model": "m",
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 7},
        }
    )

    assert result.provenance.usage.input_tokens == 7
    assert result.provenance.usage.output_tokens == UNKNOWN
    assert result.provenance.usage.total_tokens == UNKNOWN


@pytest.mark.asyncio
async def test_infer_text_path_is_unchanged() -> None:
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as post:
        post.return_value = _completion({"choices": [{"message": {"content": "ok"}}]})
        text = await _bridge().infer(
            model_key=_ROUTE, system_prompt="s", user_prompt="u", timeout_seconds=5.0
        )
    assert text == "ok"


@pytest.mark.asyncio
async def test_adapter_that_reports_nothing_reads_all_unknown() -> None:
    class _TextOnly(ModelInferenceAdapter):
        async def infer(
            self,
            model_key: str,
            system_prompt: str,
            user_prompt: str,
            timeout_seconds: float,
            temperature: float | None = None,
            response_format: ModelInferenceJsonObjectResponseFormat | None = None,
            protocol_selection: ModelInferenceProtocolSelection | None = None,
        ) -> str:
            return "text"

    result = await _TextOnly().infer_with_provenance(
        model_key="k", system_prompt="s", user_prompt="u", timeout_seconds=1.0
    )

    assert result.text == "text"
    assert result.provenance == ModelInferenceProvenance()
    assert result.provenance.served_model == UNKNOWN


@pytest.mark.asyncio
async def test_delegation_route_reads_provider_model_and_metrics() -> None:
    response = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=uuid4(),
        task_type="review",
        response="[]",
        provider="lab",
        model_name="qwen-served",
        metrics=ModelDelegateSkillResponseMetrics(
            input_tokens=11, output_tokens=5, total_tokens=16
        ),
    )
    handler = MagicMock()
    handler.handle = AsyncMock(return_value=response)

    result = await HandlerDelegationReviewInference(handler).infer_with_provenance(
        model_key="route",
        system_prompt="s",
        user_prompt="u",
        timeout_seconds=30.0,
        temperature=0.1,
    )

    assert result.text == "[]"
    assert result.provenance.served_model == "qwen-served"
    assert result.provenance.settings.temperature == 0.1
    assert result.provenance.usage == ModelServedUsage(
        input_tokens=11, output_tokens=5, total_tokens=16
    )


@pytest.mark.asyncio
async def test_delegation_route_without_a_model_name_reads_unknown() -> None:
    response = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=uuid4(),
        task_type="review",
        response="[]",
    )
    handler = MagicMock()
    handler.handle = AsyncMock(return_value=response)

    result = await HandlerDelegationReviewInference(handler).infer_with_provenance(
        model_key="route", system_prompt="s", user_prompt="u", timeout_seconds=30.0
    )

    assert result.provenance.served_model == UNKNOWN
    assert result.provenance.settings.temperature == UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{}, {"metrics": None}, {"model_name": "served-model"}],
)
async def test_text_only_delegation_response_reads_missing_provenance_as_unknown(
    metadata: dict[str, object],
) -> None:
    handler = MagicMock()
    handler.handle = AsyncMock(
        return_value=SimpleNamespace(
            status="completed", response="reviewed", error_message=None, **metadata
        )
    )

    result = await HandlerDelegationReviewInference(handler).infer_with_provenance(
        model_key="requested-route",
        system_prompt="s",
        user_prompt="u",
        timeout_seconds=30.0,
        temperature=0.0,
    )

    assert result.text == "reviewed"
    assert result.provenance.served_model == metadata.get("model_name", UNKNOWN)
    assert result.provenance.usage == ModelServedUsage()
    assert result.provenance.settings == ModelServedSettings(temperature=0.0)
