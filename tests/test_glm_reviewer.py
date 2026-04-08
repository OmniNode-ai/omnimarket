# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for GLM-4.7-Flash reviewer wiring (OMN-7856).

Covers:
- FRONTIER_REVIEW tier in EnumModelTier
- GLM-4.7-Flash endpoint registered in build_endpoint_configs when LLM_GLM_API_KEY is set
- ModelReviewResult schema validation
- _parse_review_response handles JSON, markdown-fenced JSON, and malformed responses
- review_unavailable is a distinct state, never auto-approved
- malformed review retries once then returns "failed"
- _review_plan returns correct status for each case
- allow_unreviewed=False (default) rejects unavailable reviews
- allow_unreviewed=True accepts unavailable reviews
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
    build_endpoint_configs,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    AdapterLlmDispatch,
    ModelReviewResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelDispatchTrace,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_review_endpoint() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        tier=EnumModelTier.FRONTIER_REVIEW,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model_id="glm-4.7-flash",
        api_key="test-key",
        max_tokens=2048,
        context_window=203000,
        timeout_seconds=30.0,
    )


def _make_target(ticket_id: str = "OMN-TEST") -> BuildTarget:
    return BuildTarget(
        ticket_id=ticket_id,
        title="Test ticket",
        buildability="auto_buildable",
    )


def _make_adapter(allow_unreviewed: bool = False) -> AdapterLlmDispatch:
    endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] = {
        EnumModelTier.FRONTIER_REVIEW: _make_review_endpoint(),
        EnumModelTier.LOCAL_CODER: ModelEndpointConfig(
            tier=EnumModelTier.LOCAL_CODER,
            base_url="http://localhost:8000",
            model_id="default",
            max_tokens=4096,
            context_window=64000,
            timeout_seconds=120.0,
        ),
    }
    return AdapterLlmDispatch(
        endpoint_configs=endpoint_configs,
        delegation_topic="test-topic",
        allow_unreviewed=allow_unreviewed,
    )


# ---------------------------------------------------------------------------
# EnumModelTier: FRONTIER_REVIEW exists
# ---------------------------------------------------------------------------


def test_frontier_review_tier_exists() -> None:
    """FRONTIER_REVIEW must be a member of EnumModelTier."""
    assert EnumModelTier.FRONTIER_REVIEW == "frontier_review"
    assert EnumModelTier.FRONTIER_REVIEW in list(EnumModelTier)


# ---------------------------------------------------------------------------
# build_endpoint_configs: GLM reviewer registered when key is set
# ---------------------------------------------------------------------------


def test_build_endpoint_configs_registers_glm_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLM-4.7-Flash endpoint registered when LLM_GLM_API_KEY is set."""
    monkeypatch.setenv("LLM_GLM_API_KEY", "test-api-key")
    monkeypatch.delenv("LLM_GLM_URL", raising=False)

    configs = build_endpoint_configs()

    assert EnumModelTier.FRONTIER_REVIEW in configs
    cfg = configs[EnumModelTier.FRONTIER_REVIEW]
    assert cfg.model_id == "glm-4.7-flash"
    assert cfg.max_tokens == 2048
    assert cfg.timeout_seconds == 30.0
    assert cfg.api_key == "test-api-key"
    assert "bigmodel.cn" in cfg.base_url


def test_build_endpoint_configs_no_reviewer_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRONTIER_REVIEW must NOT be registered when LLM_GLM_API_KEY is absent."""
    monkeypatch.delenv("LLM_GLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_GLM_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    configs = build_endpoint_configs()
    assert EnumModelTier.FRONTIER_REVIEW not in configs


def test_build_endpoint_configs_glm_review_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_GLM_URL overrides the default bigmodel.cn URL for reviewer too."""
    monkeypatch.setenv("LLM_GLM_API_KEY", "key")
    monkeypatch.setenv("LLM_GLM_URL", "https://custom.endpoint/api")

    configs = build_endpoint_configs()
    assert (
        configs[EnumModelTier.FRONTIER_REVIEW].base_url == "https://custom.endpoint/api"
    )


# ---------------------------------------------------------------------------
# ModelReviewResult schema
# ---------------------------------------------------------------------------


def test_model_review_result_approved() -> None:
    data = {"approved": True, "issues": [], "risk_level": "low"}
    result = ModelReviewResult.model_validate(data)
    assert result.approved is True
    assert result.issues == []
    assert result.risk_level == "low"


def test_model_review_result_rejected_with_issues() -> None:
    data = {
        "approved": False,
        "issues": [{"line": 15, "severity": "major", "message": "hallucinated field"}],
        "risk_level": "high",
    }
    result = ModelReviewResult.model_validate(data)
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "major"
    assert result.issues[0].line == 15


def test_model_review_result_invalid_severity() -> None:
    data = {
        "approved": False,
        "issues": [{"severity": "blocker", "message": "bad"}],
        "risk_level": "low",
    }
    with pytest.raises(ValueError, match="severity"):
        ModelReviewResult.model_validate(data)


def test_model_review_result_invalid_risk_level() -> None:
    data = {"approved": True, "issues": [], "risk_level": "critical"}
    with pytest.raises(ValueError, match="risk_level"):
        ModelReviewResult.model_validate(data)


# ---------------------------------------------------------------------------
# _parse_review_response
# ---------------------------------------------------------------------------


def test_parse_review_response_bare_json() -> None:
    raw = json.dumps({"approved": True, "issues": [], "risk_level": "low"})
    result = AdapterLlmDispatch._parse_review_response(raw)
    assert result is not None
    assert result.approved is True


def test_parse_review_response_markdown_fenced() -> None:
    raw = '```json\n{"approved": false, "issues": [{"line": 10, "severity": "major", "message": "bad"}], "risk_level": "high"}\n```'
    result = AdapterLlmDispatch._parse_review_response(raw)
    assert result is not None
    assert result.approved is False
    assert result.risk_level == "high"


def test_parse_review_response_plain_fence() -> None:
    raw = '```\n{"approved": true, "issues": [], "risk_level": "low"}\n```'
    result = AdapterLlmDispatch._parse_review_response(raw)
    assert result is not None
    assert result.approved is True


def test_parse_review_response_prose_returns_none() -> None:
    raw = "The code looks good to me! I would approve this change."
    result = AdapterLlmDispatch._parse_review_response(raw)
    assert result is None


def test_parse_review_response_malformed_json_returns_none() -> None:
    raw = '{"approved": true, "issues": [], "risk_level":}'
    result = AdapterLlmDispatch._parse_review_response(raw)
    assert result is None


# ---------------------------------------------------------------------------
# _is_accepted: review policy
# ---------------------------------------------------------------------------


def test_is_accepted_approved() -> None:
    adapter = _make_adapter()
    assert adapter._is_accepted("approved", {}) is True


def test_is_accepted_rejected() -> None:
    adapter = _make_adapter()
    assert adapter._is_accepted("rejected", {"issues": ["bad"]}) is False


def test_is_accepted_unavailable_default_policy() -> None:
    """Default: allow_unreviewed=False — unavailable review must NOT be accepted."""
    adapter = _make_adapter(allow_unreviewed=False)
    assert adapter._is_accepted("unavailable", {}) is False


def test_is_accepted_unavailable_explicit_allow() -> None:
    """allow_unreviewed=True — unavailable review may be accepted."""
    adapter = _make_adapter(allow_unreviewed=True)
    assert adapter._is_accepted("unavailable", {}) is True


def test_is_accepted_failed_always_rejected() -> None:
    adapter = _make_adapter(allow_unreviewed=True)
    assert adapter._is_accepted("failed", {}) is False


def test_is_accepted_malformed_always_rejected() -> None:
    adapter = _make_adapter(allow_unreviewed=True)
    assert adapter._is_accepted("malformed", {}) is False


# ---------------------------------------------------------------------------
# _review_plan: endpoint unreachable -> "unavailable"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_plan_endpoint_unreachable_returns_unavailable() -> None:
    """If the review endpoint raises httpx.HTTPError, status must be 'unavailable'."""
    adapter = _make_adapter()
    endpoint = _make_review_endpoint()
    target = _make_target()

    with patch.object(
        AdapterLlmDispatch,
        "_call_endpoint",
        new=AsyncMock(side_effect=httpx.ConnectError("Connection refused")),
    ):
        status, data = await adapter._review_plan(target, {}, endpoint)

    assert status == "unavailable"
    assert data.get("issues") == []


# ---------------------------------------------------------------------------
# _review_plan: malformed once -> retry -> approved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_plan_malformed_first_attempt_then_valid() -> None:
    """First attempt returns prose (malformed), second returns valid JSON -> approved."""
    adapter = _make_adapter()
    endpoint = _make_review_endpoint()
    target = _make_target()

    valid_json = json.dumps({"approved": True, "issues": [], "risk_level": "low"})

    call_count = 0

    async def _mock_call(ep: object, sp: str, up: str, temperature: float = 0.2) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "Looks good to me!"
        return valid_json

    with patch.object(AdapterLlmDispatch, "_call_endpoint", new=_mock_call):
        status, _data = await adapter._review_plan(target, {}, endpoint)

    assert call_count == 2
    assert status == "approved"


# ---------------------------------------------------------------------------
# _review_plan: malformed both attempts -> "failed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_plan_malformed_both_attempts_returns_failed() -> None:
    """Both attempts return prose — must return 'failed', never auto-approve."""
    adapter = _make_adapter()
    endpoint = _make_review_endpoint()
    target = _make_target()

    with patch.object(
        AdapterLlmDispatch,
        "_call_endpoint",
        new=AsyncMock(return_value="The code looks good, I approve!"),
    ):
        status, data = await adapter._review_plan(target, {}, endpoint)

    assert status == "failed"
    # Must NOT be treated as approved
    assert adapter._is_accepted(status, data) is False


# ---------------------------------------------------------------------------
# _review_plan: reviewer rejects -> "rejected"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_plan_reviewer_rejects() -> None:
    adapter = _make_adapter()
    endpoint = _make_review_endpoint()
    target = _make_target()

    reject_json = json.dumps(
        {
            "approved": False,
            "issues": [
                {"line": 5, "severity": "critical", "message": "wrong method name"}
            ],
            "risk_level": "high",
        }
    )

    with patch.object(
        AdapterLlmDispatch,
        "_call_endpoint",
        new=AsyncMock(return_value=reject_json),
    ):
        status, data = await adapter._review_plan(target, {}, endpoint)

    assert status == "rejected"
    assert adapter._is_accepted(status, data) is False


# ---------------------------------------------------------------------------
# handle(): no FRONTIER_REVIEW configured -> review_status="unavailable" in payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_no_reviewer_sets_unavailable_status() -> None:
    """When FRONTIER_REVIEW tier is absent, payload must record review_status=unavailable."""
    endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] = {
        EnumModelTier.LOCAL_CODER: ModelEndpointConfig(
            tier=EnumModelTier.LOCAL_CODER,
            base_url="http://localhost:8000",
            model_id="default",
            max_tokens=4096,
            context_window=64000,
            timeout_seconds=120.0,
        ),
    }
    adapter = AdapterLlmDispatch(
        endpoint_configs=endpoint_configs,
        delegation_topic="test-topic",
        allow_unreviewed=False,
    )
    targets = (
        BuildTarget(ticket_id="OMN-X", title="Test", buildability="auto_buildable"),
    )

    valid_plan: dict[str, object] = {
        "ticket_id": "OMN-X",
        "implementation_plan": {
            "approach": "add feature",
            "estimated_complexity": "low",
        },
        "code_changes": [],
    }

    stub_trace = ModelDispatchTrace(
        correlation_id=str(uuid4()),
        ticket_id="OMN-X",
        attempt=1,
        timestamp="2026-01-01T00:00:00+00:00",
        coder_model="default",
        quality_gate=ModelQualityGateResult(
            ruff_pass=True, import_pass=True, test_pass=True, errors=[]
        ),
        accepted=True,
    )
    with patch.object(
        AdapterLlmDispatch,
        "_generate_plan_traced",
        new=AsyncMock(return_value=(valid_plan, stub_trace)),
    ):
        result = await adapter.handle(
            correlation_id=uuid4(),
            targets=targets,
        )

    assert result.total_dispatched == 1
    payload = result.delegation_payloads[0].payload
    assert payload["review_status"] == "unavailable"
    assert payload["accepted"] is False
