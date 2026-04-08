# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ModelDispatchTrace and dispatch trace writing.

Tests:
- ModelQualityGateResult / ModelReviewResult / ModelDispatchTrace field validation
- _write_trace writes correct filename and JSON content
- _emit_trace_to_bus skips when KAFKA_BOOTSTRAP_SERVERS not set
- AdapterLlmDispatch._generate_with_review writes trace on success and failure
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
    ModelReviewIssue,
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
    issue = ModelReviewIssue(severity="major", message="bad field")
    r = ModelReviewResult(
        approved=False,
        issues=[issue],
        reviewer_model="glm-4.7-flash",
        review_tokens=100,
    )
    assert r.approved is False
    assert len(r.issues) == 1
    assert r.issues[0].message == "bad field"
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
# AdapterLlmDispatch._generate_with_review integration (OMN-7857)
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


_VALID_PYTHON = "def handle(req):\n    return req\n"


@pytest.mark.asyncio
async def test_generate_with_review_writes_trace_on_success(
    tmp_state_dir: Path,
) -> None:
    """Accepted attempt produces a trace file with accepted=True."""
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=tmp_state_dir,
        allow_unreviewed=True,  # no reviewer configured
    )
    target = BuildTarget(
        ticket_id="OMN-TEST", title="Test ticket", buildability="auto_buildable"
    )

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = _VALID_PYTHON
        code, traces = await adapter._generate_with_review(
            target=target,
            coder_endpoint=_make_endpoint(),
            reviewer_endpoint=None,
            template_source="def handle(req): pass",
            target_source="def run_full_pipeline(): pass",
            model_sources=[],
            max_attempts=3,
            correlation_id=corr_id,
        )

    assert code is not None
    assert len(traces) == 1
    assert traces[0].accepted is True
    assert traces[0].ticket_id == "OMN-TEST"
    assert traces[0].attempt == 1

    fname = f"{corr_id}-OMN-TEST-attempt-1.json"
    trace_path = tmp_state_dir / "dispatch-traces" / fname
    assert trace_path.exists()
    data = json.loads(trace_path.read_text())
    assert data["accepted"] is True


@pytest.mark.asyncio
async def test_generate_with_review_writes_trace_on_transport_error(
    tmp_state_dir: Path,
) -> None:
    """Transport failure produces a trace with failure_kind=transport_failure."""
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=tmp_state_dir,
        allow_unreviewed=True,
    )
    target = BuildTarget(
        ticket_id="OMN-ERR", title="Error ticket", buildability="auto_buildable"
    )

    with patch.object(
        AdapterLlmDispatch,
        "_call_endpoint",
        new_callable=AsyncMock,
        side_effect=RuntimeError("connection refused"),
    ):
        code, traces = await adapter._generate_with_review(
            target=target,
            coder_endpoint=_make_endpoint(),
            reviewer_endpoint=None,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=1,
            correlation_id=corr_id,
        )

    assert code is None
    assert len(traces) == 1
    assert traces[0].accepted is False
    assert traces[0].failure_kind == "transport_failure"
    fname = f"{corr_id}-OMN-ERR-attempt-1.json"
    assert (tmp_state_dir / "dispatch-traces" / fname).exists()
