# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ModelDispatchMetrics and dispatch metrics writing.

Tests:
- _compute_metrics produces correct totals from a list of traces
- _compute_metrics handles empty trace list gracefully
- _compute_metrics quality_gate_failure_rate and review_rejection_rate
- _write_metrics writes correct filename and JSON to .onex_state/dispatch-metrics/
- _emit_metrics_to_bus skips when KAFKA_BOOTSTRAP_SERVERS not set
- AdapterLlmDispatch.handle() writes metrics file after non-dry-run dispatch
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    AdapterLlmDispatch,
    _compute_metrics,
    _write_metrics,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_metrics import (
    ModelDispatchMetrics,
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
# Helpers
# ---------------------------------------------------------------------------


def _gate(
    pass_all: bool = True, errors: list[str] | None = None
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        ruff_pass=pass_all,
        import_pass=pass_all,
        test_pass=pass_all,
        errors=errors or [],
    )


def _review(approved: bool, tokens: int = 50) -> ModelReviewResult:
    return ModelReviewResult(
        approved=approved,
        issues=[]
        if approved
        else [ModelReviewIssue(severity="minor", message="bad code")],
        reviewer_model="glm-4.7-flash",
        review_tokens=tokens,
    )


def _make_trace(
    correlation_id: str = "corr-001",
    ticket_id: str = "OMN-1",
    attempt: int = 1,
    accepted: bool = True,
    gate_pass: bool = True,
    gate_errors: list[str] | None = None,
    review: ModelReviewResult | None = None,
    coder_model: str = "qwen3-coder-30b",
    reviewer_model: str | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
    wall_clock_ms: int = 500,
) -> ModelDispatchTrace:
    return ModelDispatchTrace(
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        attempt=attempt,
        timestamp="2026-04-08T00:00:00+00:00",
        coder_model=coder_model,
        reviewer_model=reviewer_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_chars=500,
        generation_raw="{}",
        quality_gate=_gate(gate_pass, gate_errors),
        review_result=review,
        accepted=accepted,
        wall_clock_ms=wall_clock_ms,
    )


# ---------------------------------------------------------------------------
# ModelDispatchMetrics model tests
# ---------------------------------------------------------------------------


def test_metrics_frozen() -> None:
    m = ModelDispatchMetrics(
        correlation_id="c1",
        total_tickets=1,
        accepted_count=1,
        rejected_count=0,
        total_generation_attempts=1,
        total_review_iterations=0,
        avg_attempts_per_ticket=1.0,
        total_prompt_tokens=10,
        total_completion_tokens=20,
        total_review_tokens=0,
        total_wall_clock_ms=100,
        coder_model="qwen3-coder-30b",
        reviewer_model=None,
        quality_gate_failure_rate=0.0,
        review_rejection_rate=0.0,
    )
    with pytest.raises((ValidationError, TypeError)):
        m.total_tickets = 99  # type: ignore[misc]


def test_metrics_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        ModelDispatchMetrics(
            correlation_id="c1",
            total_tickets=1,
            accepted_count=1,
            rejected_count=0,
            total_generation_attempts=1,
            total_review_iterations=0,
            avg_attempts_per_ticket=1.0,
            total_prompt_tokens=10,
            total_completion_tokens=20,
            total_review_tokens=0,
            total_wall_clock_ms=100,
            coder_model="qwen3-coder-30b",
            reviewer_model=None,
            quality_gate_failure_rate=0.0,
            review_rejection_rate=0.0,
            unexpected="oops",  # type: ignore[call-arg]
        )


def test_metrics_reviewer_model_nullable() -> None:
    m = ModelDispatchMetrics(
        correlation_id="c1",
        total_tickets=0,
        accepted_count=0,
        rejected_count=0,
        total_generation_attempts=0,
        total_review_iterations=0,
        avg_attempts_per_ticket=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_review_tokens=0,
        total_wall_clock_ms=0,
        coder_model="qwen3-coder-30b",
        reviewer_model=None,
        quality_gate_failure_rate=0.0,
        review_rejection_rate=0.0,
    )
    assert m.reviewer_model is None


# ---------------------------------------------------------------------------
# _compute_metrics tests
# ---------------------------------------------------------------------------


def test_compute_metrics_empty_traces() -> None:
    m = _compute_metrics(
        correlation_id="corr-000",
        traces=[],
    )
    assert m.total_tickets == 0
    assert m.accepted_count == 0
    assert m.rejected_count == 0
    assert m.total_generation_attempts == 0
    assert m.avg_attempts_per_ticket == 0.0
    assert m.quality_gate_failure_rate == 0.0
    assert m.review_rejection_rate == 0.0
    assert m.reviewer_model is None
    assert m.coder_model == "none"


def test_compute_metrics_single_accepted_trace() -> None:
    trace = _make_trace(
        correlation_id="c1",
        ticket_id="OMN-1",
        attempt=1,
        accepted=True,
        gate_pass=True,
        prompt_tokens=100,
        completion_tokens=200,
        wall_clock_ms=400,
    )
    m = _compute_metrics(correlation_id="c1", traces=[trace])

    assert m.total_tickets == 1
    assert m.accepted_count == 1
    assert m.rejected_count == 0
    assert m.total_generation_attempts == 1
    assert m.avg_attempts_per_ticket == 1.0
    assert m.total_prompt_tokens == 100
    assert m.total_completion_tokens == 200
    assert m.total_wall_clock_ms == 400
    assert m.quality_gate_failure_rate == 0.0
    assert m.review_rejection_rate == 0.0


def test_compute_metrics_all_rejected() -> None:
    traces = [
        _make_trace(ticket_id="OMN-1", attempt=1, accepted=False, gate_pass=False),
        _make_trace(ticket_id="OMN-2", attempt=1, accepted=False, gate_pass=False),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    assert m.total_tickets == 2
    assert m.accepted_count == 0
    assert m.rejected_count == 2


def test_compute_metrics_multiple_attempts_per_ticket() -> None:
    """Two failed attempts then one accepted for a single ticket."""
    traces = [
        _make_trace(ticket_id="OMN-5", attempt=1, accepted=False, gate_pass=False),
        _make_trace(ticket_id="OMN-5", attempt=2, accepted=False, gate_pass=False),
        _make_trace(ticket_id="OMN-5", attempt=3, accepted=True, gate_pass=True),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    assert m.total_tickets == 1
    assert m.accepted_count == 1
    assert m.total_generation_attempts == 3
    assert m.avg_attempts_per_ticket == 3.0


def test_compute_metrics_token_totals() -> None:
    traces = [
        _make_trace(ticket_id="OMN-1", prompt_tokens=100, completion_tokens=50),
        _make_trace(ticket_id="OMN-2", prompt_tokens=200, completion_tokens=75),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    assert m.total_prompt_tokens == 300
    assert m.total_completion_tokens == 125


def test_compute_metrics_review_tokens_summed() -> None:
    traces = [
        _make_trace(
            ticket_id="OMN-1",
            gate_pass=True,
            accepted=True,
            review=_review(approved=True, tokens=60),
            reviewer_model="glm-4.7-flash",
        ),
        _make_trace(
            ticket_id="OMN-2",
            gate_pass=True,
            accepted=True,
            review=_review(approved=True, tokens=40),
            reviewer_model="glm-4.7-flash",
        ),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    assert m.total_review_tokens == 100
    assert m.total_review_iterations == 2
    assert m.reviewer_model == "glm-4.7-flash"


def test_compute_metrics_quality_gate_failure_rate() -> None:
    """3 attempts: 2 gate failures (no review), 1 gate pass (accepted)."""
    traces = [
        # Gate fails — never reached review
        _make_trace(
            ticket_id="OMN-1",
            attempt=1,
            accepted=False,
            gate_pass=False,
            review=None,
        ),
        _make_trace(
            ticket_id="OMN-1",
            attempt=2,
            accepted=False,
            gate_pass=False,
            review=None,
        ),
        # Gate passes — accepted
        _make_trace(
            ticket_id="OMN-1",
            attempt=3,
            accepted=True,
            gate_pass=True,
            review=None,
        ),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    # 2/3 attempts failed the gate
    assert abs(m.quality_gate_failure_rate - 2 / 3) < 1e-9


def test_compute_metrics_review_rejection_rate() -> None:
    """3 gate-passing attempts: 2 reviewer-rejected, 1 approved."""
    traces = [
        _make_trace(
            ticket_id="OMN-1",
            attempt=1,
            accepted=False,
            gate_pass=True,
            review=_review(approved=False),
        ),
        _make_trace(
            ticket_id="OMN-1",
            attempt=2,
            accepted=False,
            gate_pass=True,
            review=_review(approved=False),
        ),
        _make_trace(
            ticket_id="OMN-1",
            attempt=3,
            accepted=True,
            gate_pass=True,
            review=_review(approved=True),
        ),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)

    # gate_failure_rate: 0 gate failures out of 3
    assert m.quality_gate_failure_rate == 0.0
    # review_rejection_rate: 2 rejected out of 3 gate-passing
    assert abs(m.review_rejection_rate - 2 / 3) < 1e-9


def test_compute_metrics_review_unavailable_counts_as_rejection() -> None:
    """Single ticket, single gate-passing attempt with failure_kind=review_unavailable.

    DoD (OMN-8499): review_rejection_rate must be 1.0, not 0.0.
    Asserts on serialized JSON value to match ticket dod_evidence.
    """
    trace = ModelDispatchTrace(
        correlation_id="corr-avail",
        ticket_id="OMN-1",
        attempt=1,
        timestamp="2026-04-11T00:00:00+00:00",
        coder_model="qwen3-coder-30b",
        reviewer_model=None,
        prompt_tokens=100,
        completion_tokens=200,
        prompt_chars=500,
        generation_raw="{}",
        quality_gate=_gate(pass_all=True),
        review_result=None,
        accepted=False,
        wall_clock_ms=500,
        failure_kind="review_unavailable",
    )
    m = _compute_metrics(correlation_id="corr-avail", traces=[trace])

    assert m.rejected_count == 1
    assert m.total_tickets == 1
    # Serialized JSON side-effect assertion (matches dod_evidence)
    data = json.loads(m.model_dump_json())
    assert data["review_rejection_rate"] == 1.0, (
        f"Expected review_rejection_rate=1.0 for review_unavailable outcome, got {data['review_rejection_rate']}"
    )


def test_compute_metrics_reviewer_model_from_first_trace() -> None:
    """reviewer_model taken from first trace that has it."""
    traces = [
        _make_trace(ticket_id="OMN-1", reviewer_model=None),
        _make_trace(ticket_id="OMN-2", reviewer_model="glm-4.7-flash"),
        _make_trace(ticket_id="OMN-3", reviewer_model="other-model"),
    ]
    m = _compute_metrics(correlation_id="c1", traces=traces)
    assert m.reviewer_model == "glm-4.7-flash"


def test_compute_metrics_correlation_id_preserved() -> None:
    m = _compute_metrics(
        correlation_id="specific-corr-id",
        traces=[_make_trace()],
    )
    assert m.correlation_id == "specific-corr-id"


# ---------------------------------------------------------------------------
# _write_metrics tests
# ---------------------------------------------------------------------------


def test_write_metrics_creates_file(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    m = _compute_metrics(
        correlation_id="test-corr-write",
        traces=[_make_trace()],
    )
    _write_metrics(m, state_dir)

    metrics_dir = state_dir / "dispatch-metrics"
    assert metrics_dir.exists()
    expected = metrics_dir / "test-corr-write.json"
    assert expected.exists()


def test_write_metrics_filename_is_correlation_id(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    corr = "unique-corr-xyz"
    m = _compute_metrics(correlation_id=corr, traces=[])
    _write_metrics(m, state_dir)

    assert (state_dir / "dispatch-metrics" / f"{corr}.json").exists()


def test_write_metrics_json_content_valid(tmp_path: Path) -> None:
    state_dir = tmp_path / ".onex_state"
    traces = [
        _make_trace(ticket_id="OMN-1", accepted=True, prompt_tokens=100),
        _make_trace(ticket_id="OMN-2", accepted=False, gate_pass=False),
    ]
    m = _compute_metrics(
        correlation_id="content-check",
        traces=traces,
    )
    _write_metrics(m, state_dir)

    content = (state_dir / "dispatch-metrics" / "content-check.json").read_text()
    data = json.loads(content)
    assert data["correlation_id"] == "content-check"
    assert data["total_tickets"] == 2
    assert data["accepted_count"] == 1
    assert data["rejected_count"] == 1
    assert data["coder_model"] == "qwen3-coder-30b"
    assert "quality_gate_failure_rate" in data
    assert "review_rejection_rate" in data


def test_write_metrics_totals_match_traces(tmp_path: Path) -> None:
    """Verify cross-reference invariant: totals in metrics match trace sums."""
    state_dir = tmp_path / ".onex_state"
    traces = [
        _make_trace(
            ticket_id="OMN-1",
            attempt=1,
            accepted=False,
            gate_pass=False,
            prompt_tokens=50,
            completion_tokens=10,
        ),
        _make_trace(
            ticket_id="OMN-1",
            attempt=2,
            accepted=True,
            gate_pass=True,
            prompt_tokens=60,
            completion_tokens=20,
        ),
        _make_trace(
            ticket_id="OMN-2",
            attempt=1,
            accepted=True,
            gate_pass=True,
            prompt_tokens=70,
            completion_tokens=30,
        ),
    ]
    m = _compute_metrics(
        correlation_id="cross-ref-check",
        traces=traces,
    )
    _write_metrics(m, state_dir)

    content = (state_dir / "dispatch-metrics" / "cross-ref-check.json").read_text()
    data = json.loads(content)

    # total_generation_attempts matches len(traces)
    assert data["total_generation_attempts"] == len(traces)
    # total_prompt_tokens matches sum
    assert data["total_prompt_tokens"] == sum(t.prompt_tokens for t in traces)
    # accepted_count: both OMN-1 and OMN-2 have at least one accepted attempt
    assert data["accepted_count"] == 2


# ---------------------------------------------------------------------------
# _emit_metrics_to_bus: skip when KAFKA_BOOTSTRAP_SERVERS not set
# ---------------------------------------------------------------------------


def test_emit_metrics_skips_without_kafka_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    m = _compute_metrics(correlation_id="c1", traces=[])
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
        _emit_metrics_to_bus,
    )

    _emit_metrics_to_bus(m)  # no-op, no exception


# ---------------------------------------------------------------------------
# AdapterLlmDispatch.handle() integration — metrics written after dispatch
# ---------------------------------------------------------------------------


def _make_endpoint(model_id: str = "mock-model") -> ModelEndpointConfig:
    return ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_CODER,
        base_url="http://localhost:8000",
        model_id=model_id,
        max_tokens=512,
        timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_handle_writes_metrics_file_after_dispatch(tmp_path: Path) -> None:
    """After a non-dry-run dispatch, metrics file must exist."""
    state_dir = tmp_path / ".onex_state"
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=state_dir,
    )
    targets = (
        BuildTarget(ticket_id="OMN-A", title="Ticket A", buildability="auto_buildable"),
        BuildTarget(ticket_id="OMN-B", title="Ticket B", buildability="auto_buildable"),
    )
    from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
        ModelQualityGateResult,
    )

    async def _fake_generate_with_review(
        self,
        *,
        target,
        coder_endpoint,
        reviewer_endpoint,
        template_source,
        target_source,
        model_sources,
        max_attempts=3,
        correlation_id,
    ):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
            ModelDispatchTrace,
        )

        trace = ModelDispatchTrace(
            correlation_id=str(correlation_id),
            ticket_id=target.ticket_id,
            attempt=1,
            timestamp="2026-04-08T00:00:00+00:00",
            coder_model="mock-model",
            reviewer_model=None,
            prompt_tokens=10,
            completion_tokens=20,
            prompt_chars=100,
            generation_raw="{}",
            quality_gate=ModelQualityGateResult(
                ruff_pass=True, import_pass=True, test_pass=True, errors=[]
            ),
            review_result=None,
            accepted=True,
            wall_clock_ms=50,
        )
        return "def main(): pass", [trace]

    with patch.object(
        AdapterLlmDispatch, "_generate_with_review", new=_fake_generate_with_review
    ):
        await adapter.handle(correlation_id=corr_id, targets=targets, dry_run=False)

    metrics_file = state_dir / "dispatch-metrics" / f"{corr_id}.json"
    assert metrics_file.exists(), f"Expected metrics file at {metrics_file}"

    data = json.loads(metrics_file.read_text())
    assert data["correlation_id"] == str(corr_id)
    assert data["total_tickets"] == 2
    assert data["total_generation_attempts"] == 2


@pytest.mark.asyncio
async def test_handle_dry_run_does_not_write_metrics(tmp_path: Path) -> None:
    """Dry-run dispatches do not write metrics (no LLM calls, no traces)."""
    state_dir = tmp_path / ".onex_state"
    corr_id = uuid.uuid4()
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_endpoint()},
        state_dir=state_dir,
    )
    targets = (
        BuildTarget(ticket_id="OMN-DRY", title="Dry", buildability="auto_buildable"),
    )
    await adapter.handle(correlation_id=corr_id, targets=targets, dry_run=True)

    metrics_dir = state_dir / "dispatch-metrics"
    assert not metrics_dir.exists() or not any(metrics_dir.iterdir())
