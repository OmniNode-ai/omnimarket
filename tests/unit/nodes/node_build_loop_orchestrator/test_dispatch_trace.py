# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ModelDispatchTrace and dispatch trace writing.

Tests:
- ModelQualityGateResult / ModelReviewResult / ModelDispatchTrace field validation
- _write_trace writes correct filename and JSON content
- _emit_trace_to_bus skips when KAFKA_BOOTSTRAP_SERVERS not set
- AdapterLlmDispatch._generate_plan_traced writes trace on LLM success and failure
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    AdapterLlmDispatch,
    _write_trace,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelDispatchTrace,
    ModelQualityGateResult,
    ModelReviewResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)

# ---------------------------------------------------------------------------
# Model unit tests
# ---------------------------------------------------------------------------


def test_quality_gate_all_pass() -> None:
    gate = ModelQualityGateResult(ruff_pass=True, import_pass=True, test_pass=True)
    assert gate.all_pass is True


def test_quality_gate_fails_when_any_false() -> None:
    gate = ModelQualityGateResult(ruff_pass=True, import_pass=False, test_pass=True)
    assert gate.all_pass is False


def test_quality_gate_errors_default_empty() -> None:
    gate = ModelQualityGateResult(ruff_pass=True, import_pass=True, test_pass=True)
    assert gate.errors == []


def test_review_result_fields() -> None:
    r = ModelReviewResult(
        approved=False,
        issues=["bad"],
        reviewer_model="glm-4.7-flash",
        review_tokens=100,
    )
    assert r.approved is False
    assert r.issues == ["bad"]
    assert r.review_tokens == 100


def test_dispatch_trace_frozen() -> None:
    gate = ModelQualityGateResult(ruff_pass=True, import_pass=True, test_pass=True)
    trace = ModelDispatchTrace(
        correlation_id="abc",
        ticket_id="OMN-1",
        attempt=1,
        timestamp="2026-04-08T00:00:00+00:00",
        coder_model="qwen3-30b",
        prompt_tokens=0,
        completion_tokens=0,
        prompt_chars=100,
        generation_raw="{}",
        quality_gate=gate,
        accepted=True,
        wall_clock_ms=500,
    )
    with pytest.raises(ValidationError):
        trace.accepted = False  # type: ignore[misc]


def test_dispatch_trace_extra_fields_forbidden() -> None:
    gate = ModelQualityGateResult(ruff_pass=True, import_pass=True, test_pass=True)
    with pytest.raises(ValidationError):
        ModelDispatchTrace(
            correlation_id="abc",
            ticket_id="OMN-1",
            attempt=1,
            timestamp="2026-04-08T00:00:00+00:00",
            coder_model="qwen3-30b",
            prompt_tokens=0,
            completion_tokens=0,
            prompt_chars=100,
            generation_raw="{}",
            quality_gate=gate,
            accepted=True,
            wall_clock_ms=500,
            unexpected_field="oops",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# _write_trace tests
# ---------------------------------------------------------------------------


def _make_trace(
    correlation_id: str = "corr-123",
    ticket_id: str = "OMN-9999",
    attempt: int = 1,
    accepted: bool = True,
) -> ModelDispatchTrace:
    gate = ModelQualityGateResult(
        ruff_pass=accepted, import_pass=accepted, test_pass=accepted
    )
    return ModelDispatchTrace(
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        attempt=attempt,
        timestamp="2026-04-08T00:00:00+00:00",
        coder_model="qwen3-coder-30b",
        prompt_tokens=10,
        completion_tokens=20,
        prompt_chars=200,
        generation_raw='{"ticket_id": "OMN-9999"}',
        quality_gate=gate,
        accepted=accepted,
        wall_clock_ms=123,
    )


def test_write_trace_creates_file(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    trace = _make_trace()
    _write_trace(trace, state_dir)

    traces_dir = state_dir / "dispatch-traces"
    assert traces_dir.exists()
    expected = traces_dir / "corr-123-OMN-9999-attempt-1.json"
    assert expected.exists()


def test_write_trace_filename_includes_all_parts(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    trace = _make_trace(correlation_id="cid-XYZ", ticket_id="OMN-42", attempt=3)
    _write_trace(trace, state_dir)

    fname = "cid-XYZ-OMN-42-attempt-3.json"
    assert (state_dir / "dispatch-traces" / fname).exists()


def test_write_trace_json_content_valid(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    trace = _make_trace()
    _write_trace(trace, state_dir)

    content = (
        state_dir / "dispatch-traces" / "corr-123-OMN-9999-attempt-1.json"
    ).read_text()
    data = json.loads(content)
    assert data["correlation_id"] == "corr-123"
    assert data["ticket_id"] == "OMN-9999"
    assert data["attempt"] == 1
    assert data["accepted"] is True
    assert "quality_gate" in data


def test_write_trace_fail_trace_has_errors(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    trace = _make_trace(accepted=False)
    _write_trace(trace, state_dir)

    content = (
        state_dir / "dispatch-traces" / "corr-123-OMN-9999-attempt-1.json"
    ).read_text()
    data = json.loads(content)
    assert data["accepted"] is False
    assert data["quality_gate"]["ruff_pass"] is False


def test_write_trace_idempotent_overwrite(tmp_path: Path) -> None:
    """Writing the same trace twice overwrites — last write wins."""
    state_dir = tmp_path / ".onex_state"
    trace = _make_trace()
    _write_trace(trace, state_dir)
    _write_trace(trace, state_dir)  # no error


# ---------------------------------------------------------------------------
# _emit_trace_to_bus: skip when KAFKA_BOOTSTRAP_SERVERS not set
# ---------------------------------------------------------------------------


def test_emit_trace_skips_without_kafka_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No import attempted when KAFKA_BOOTSTRAP_SERVERS is absent."""
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    trace = _make_trace()
    # Should not raise even though omnibase_infra may not be installed
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
        _emit_trace_to_bus,
    )

    _emit_trace_to_bus(trace)  # no-op, no exception


# ---------------------------------------------------------------------------
# AdapterLlmDispatch._generate_plan_traced integration
# ---------------------------------------------------------------------------


def _make_endpoint(model_id: str = "mock-model") -> ModelEndpointConfig:
    return ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_CODER,
        base_url="http://localhost:8000",
        model_id=model_id,
        max_tokens=512,
        timeout_seconds=5.0,
    )


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".onex_state"


def _make_mock_router(response_text: str, model_name: str = "mock-model") -> AsyncMock:
    """Build a mock AdapterModelRouter that returns the given response text."""
    from omnibase_infra.adapters.llm.model_llm_adapter_response import (
        ModelLlmAdapterResponse,
    )

    mock_router = AsyncMock()
    mock_router.get_available_providers = AsyncMock(return_value=["local_coder"])
    mock_router.generate_typed = AsyncMock(
        return_value=ModelLlmAdapterResponse(
            generated_text=response_text,
            model_used=model_name,
            usage_statistics={"prompt_tokens": 10, "completion_tokens": 20},
        )
    )
    return mock_router


@pytest.mark.asyncio
async def test_generate_plan_traced_writes_trace_on_success(
    tmp_state_dir: Path,
) -> None:
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=tmp_state_dir,
    )
    target = BuildTarget(
        ticket_id="OMN-TEST", title="Test ticket", buildability="auto_buildable"
    )
    valid_json = json.dumps({"ticket_id": "OMN-TEST", "implementation_plan": {}})

    with patch.object(
        AdapterLlmDispatch,
        "_ensure_router",
        new_callable=AsyncMock,
        return_value=_make_mock_router(valid_json),
    ):
        _plan, trace = await adapter._generate_plan_traced(
            target=target,
            correlation_id=corr_id,
            attempt=1,
        )

    assert trace.accepted is True
    assert trace.ticket_id == "OMN-TEST"
    assert trace.attempt == 1
    assert trace.quality_gate.all_pass is True

    # Trace file must exist
    fname = f"{corr_id}-OMN-TEST-attempt-1.json"
    trace_path = tmp_state_dir / "dispatch-traces" / fname
    assert trace_path.exists()
    data = json.loads(trace_path.read_text())
    assert data["accepted"] is True


@pytest.mark.asyncio
async def test_generate_plan_traced_writes_trace_on_json_failure(
    tmp_state_dir: Path,
) -> None:
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=tmp_state_dir,
    )
    target = BuildTarget(
        ticket_id="OMN-FAIL", title="Bad ticket", buildability="auto_buildable"
    )

    with patch.object(
        AdapterLlmDispatch,
        "_ensure_router",
        new_callable=AsyncMock,
        return_value=_make_mock_router("not json at all — just prose"),
    ):
        _plan, trace = await adapter._generate_plan_traced(
            target=target,
            correlation_id=corr_id,
            attempt=2,
        )

    assert trace.accepted is False
    assert trace.attempt == 2
    assert any("JSON" in e for e in trace.quality_gate.errors)

    fname = f"{corr_id}-OMN-FAIL-attempt-2.json"
    assert (tmp_state_dir / "dispatch-traces" / fname).exists()


@pytest.mark.asyncio
async def test_generate_plan_traced_writes_trace_on_llm_error(
    tmp_state_dir: Path,
) -> None:
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=tmp_state_dir,
    )
    target = BuildTarget(
        ticket_id="OMN-ERR", title="Error ticket", buildability="auto_buildable"
    )

    mock_router = AsyncMock()
    mock_router.get_available_providers = AsyncMock(return_value=["local_coder"])
    mock_router.generate_typed = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )

    with patch.object(
        AdapterLlmDispatch,
        "_ensure_router",
        new_callable=AsyncMock,
        return_value=mock_router,
    ):
        _plan, trace = await adapter._generate_plan_traced(
            target=target,
            correlation_id=corr_id,
            attempt=1,
        )

    assert trace.accepted is False
    assert any("LLM call failed" in e for e in trace.quality_gate.errors)
    fname = f"{corr_id}-OMN-ERR-attempt-1.json"
    assert (tmp_state_dir / "dispatch-traces" / fname).exists()
