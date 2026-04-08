# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the generate-review-retry loop (OMN-7857).

Covers:
- _generate_with_review: max 3 attempts, all attempts traced
- Quality gate (ruff + syntax) catches bad output
- Review feedback fed back to coder on retry (replaced, not accumulated)
- Returns None after all attempts exhausted
- Failure taxonomy in traces: gate_failed, review_rejected, review_unavailable, transport_failure, generation_malformed
- _run_quality_gate: ruff + AST pass/fail
- _extract_code_from_response: markdown fence stripping
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    _FAILURE_GATE_FAILED,
    _FAILURE_GENERATION_MALFORMED,
    _FAILURE_REVIEW_REJECTED,
    _FAILURE_REVIEW_UNAVAILABLE,
    AdapterLlmDispatch,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelReviewResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_PYTHON = "def handle(req):\n    return req\n"
_SYNTAX_ERROR_PYTHON = (
    "def handle(req):\n    return req\n    orphan syntax error @@@@\n"
)
_EMPTY_RESPONSE = "   "


def _make_coder_endpoint() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_CODER,
        base_url="http://localhost:8000",
        model_id="qwen3-coder-30b",
        max_tokens=4096,
        timeout_seconds=30.0,
    )


def _make_reviewer_endpoint() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        tier=EnumModelTier.FRONTIER_REVIEW,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model_id="glm-4.7-flash",
        api_key="test-key",
        max_tokens=2048,
        context_window=203000,
        timeout_seconds=30.0,
    )


def _make_adapter(
    tmp_path: Path,
    allow_unreviewed: bool = False,
    max_attempts: int = 3,
) -> AdapterLlmDispatch:
    return AdapterLlmDispatch(
        endpoint_configs={
            EnumModelTier.LOCAL_CODER: _make_coder_endpoint(),
            EnumModelTier.FRONTIER_REVIEW: _make_reviewer_endpoint(),
        },
        delegation_topic="test-topic",
        state_dir=tmp_path / ".onex_state",
        max_attempts=max_attempts,
        allow_unreviewed=allow_unreviewed,
    )


def _make_target(ticket_id: str = "OMN-TEST") -> BuildTarget:
    return BuildTarget(
        ticket_id=ticket_id,
        title="Test ticket",
        buildability="auto_buildable",
    )


def _approve_json() -> str:
    return json.dumps({"approved": True, "issues": [], "risk_level": "low"})


def _reject_json(message: str = "wrong method name") -> str:
    return json.dumps(
        {
            "approved": False,
            "issues": [{"line": 5, "severity": "major", "message": message}],
            "risk_level": "high",
        }
    )


# ---------------------------------------------------------------------------
# _extract_code_from_response
# ---------------------------------------------------------------------------


def test_extract_code_strips_python_fence() -> None:
    raw = "Here is the code:\n```python\ndef handle(): pass\n```\nDone."
    code = AdapterLlmDispatch._extract_code_from_response(raw)
    assert code.strip() == "def handle(): pass"
    assert "```" not in code


def test_extract_code_strips_generic_fence() -> None:
    raw = "```\ndef handle(): pass\n```"
    code = AdapterLlmDispatch._extract_code_from_response(raw)
    assert code.strip() == "def handle(): pass"


def test_extract_code_returns_raw_when_no_fence() -> None:
    raw = "def handle(): pass"
    code = AdapterLlmDispatch._extract_code_from_response(raw)
    assert code == raw


def test_extract_code_prefers_python_fence_over_generic() -> None:
    raw = "```\ngeneric\n```\n\n```python\ndef handle(): pass\n```"
    code = AdapterLlmDispatch._extract_code_from_response(raw)
    assert "def handle" in code


# ---------------------------------------------------------------------------
# _run_quality_gate
# ---------------------------------------------------------------------------


def test_quality_gate_passes_valid_python() -> None:
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_coder_endpoint()},
        delegation_topic="test",
    )
    result = adapter._run_quality_gate(_VALID_PYTHON)
    assert result.import_pass is True
    # ruff may or may not be available — don't assert ruff_pass


def test_quality_gate_fails_syntax_error() -> None:
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_coder_endpoint()},
        delegation_topic="test",
    )
    bad_code = "def handle(\n    this is not valid python @@@"
    result = adapter._run_quality_gate(bad_code)
    assert result.import_pass is False
    assert result.ruff_pass is False
    assert any("Syntax" in e or "syntax" in e for e in result.errors)


def test_quality_gate_all_pass_is_false_on_syntax_error() -> None:
    adapter = AdapterLlmDispatch(
        endpoint_configs={EnumModelTier.LOCAL_CODER: _make_coder_endpoint()},
        delegation_topic="test",
    )
    result = adapter._run_quality_gate("not valid syntax @@@")
    assert result.all_pass is False


# ---------------------------------------------------------------------------
# _generate_with_review: accepted on first attempt (no reviewer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_accepted_first_attempt_no_reviewer(tmp_path: Path) -> None:
    """With no reviewer and allow_unreviewed=True, valid code is accepted on attempt 1."""
    adapter = _make_adapter(tmp_path, allow_unreviewed=True)

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = _VALID_PYTHON
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=None,
            template_source="def handle(req): pass",
            target_source="def run_full_pipeline(): pass",
            model_sources=[],
            max_attempts=3,
            correlation_id=uuid.uuid4(),
        )

    assert code is not None
    assert len(traces) == 1
    assert traces[0].accepted is True
    assert traces[0].attempt == 1
    assert traces[0].failure_kind is None


# ---------------------------------------------------------------------------
# _generate_with_review: retry after gate failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_retries_after_gate_failure(tmp_path: Path) -> None:
    """First attempt fails quality gate, second attempt passes."""
    adapter = _make_adapter(tmp_path, allow_unreviewed=True)

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.side_effect = ["not valid python @@@", _VALID_PYTHON]
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=None,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=3,
            correlation_id=uuid.uuid4(),
        )

    assert code is not None
    assert len(traces) == 2
    assert traces[0].accepted is False
    assert traces[0].failure_kind == _FAILURE_GATE_FAILED
    assert traces[1].accepted is True
    assert traces[1].attempt == 2


# ---------------------------------------------------------------------------
# _generate_with_review: retry after review rejection, feedback replaced (not accumulated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_retries_after_review_rejection(tmp_path: Path) -> None:
    """First attempt rejected by reviewer, second attempt approved."""
    adapter = _make_adapter(tmp_path)
    reviewer_ep = _make_reviewer_endpoint()

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        # Calls: 1=coder, 2=reviewer(reject), 3=coder(retry), 4=reviewer(approve)
        mock_call.side_effect = [
            _VALID_PYTHON,
            _reject_json("wrong method name"),
            _VALID_PYTHON,
            _approve_json(),
        ]
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=reviewer_ep,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=3,
            correlation_id=uuid.uuid4(),
        )

    assert code is not None
    assert len(traces) == 2
    assert traces[0].failure_kind == _FAILURE_REVIEW_REJECTED
    assert traces[1].accepted is True


@pytest.mark.asyncio
async def test_feedback_is_replaced_not_accumulated(tmp_path: Path) -> None:
    """Review feedback on retry is the LATEST rejection, not all prior rejections concatenated.

    Strategy: capture the user_prompt argument to _call_endpoint for coder calls.
    Coder calls are every other call (1, 3, 5); reviewer calls are (2, 4, 6).
    """
    adapter = _make_adapter(tmp_path)
    reviewer_ep = _make_reviewer_endpoint()
    all_user_prompts: list[str] = []
    # Calls: coder1, reviewer(reject A), coder2, reviewer(reject B), coder3, reviewer(approve)
    _response_queue = [
        _VALID_PYTHON,
        _reject_json("issue-A"),
        _VALID_PYTHON,
        _reject_json("issue-B"),
        _VALID_PYTHON,
        _approve_json(),
    ]
    _call_idx = 0

    async def _recording_side_effect(*args: object, **kw: object) -> str:
        nonlocal _call_idx
        # user_prompt is the 3rd positional arg (endpoint, system_prompt, user_prompt)
        if len(args) >= 3:
            all_user_prompts.append(str(args[2]))
        response = _response_queue[_call_idx]
        _call_idx += 1
        return response  # type: ignore[return-value]

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.side_effect = _recording_side_effect
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=reviewer_ep,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=3,
            correlation_id=uuid.uuid4(),
        )

    # Verify: 3 traces, final accepted
    assert code is not None
    assert len(traces) == 3
    assert traces[0].failure_kind == _FAILURE_REVIEW_REJECTED
    assert traces[1].failure_kind == _FAILURE_REVIEW_REJECTED
    assert traces[2].accepted is True

    # Verify feedback replacement: each rejected trace has review_result with issues
    assert traces[0].review_result is not None
    assert any("issue-A" in i.message for i in traces[0].review_result.issues)
    assert traces[1].review_result is not None
    assert any("issue-B" in i.message for i in traces[1].review_result.issues)

    # Verify prompt replacement (not accumulation):
    # Coder calls are at indices 0, 2, 4 in all_user_prompts.
    coder_prompts = [p for i, p in enumerate(all_user_prompts) if i % 2 == 0]
    assert len(coder_prompts) >= 3
    assert "issue-A" in coder_prompts[1], (
        "2nd coder prompt must contain issue-A feedback"
    )
    assert "issue-B" in coder_prompts[2], (
        "3rd coder prompt must contain issue-B feedback"
    )
    assert "issue-A" not in coder_prompts[2], (
        "3rd coder prompt must NOT contain issue-A (replaced)"
    )


# ---------------------------------------------------------------------------
# _generate_with_review: all attempts exhausted -> returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_none_when_all_attempts_fail(tmp_path: Path) -> None:
    """Returns None and all traces when max_attempts exhausted."""
    from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
        ModelQualityGateResult,
        ModelReviewIssue,
    )

    adapter = _make_adapter(tmp_path, max_attempts=3)
    reviewer_ep = _make_reviewer_endpoint()

    async def always_reject(
        self_: object, **kw: object
    ) -> tuple[str, ModelReviewResult | None]:
        return "rejected", ModelReviewResult(
            approved=False,
            issues=[ModelReviewIssue(severity="major", message="always bad")],
            risk_level="high",
        )

    with (
        patch.object(AdapterLlmDispatch, "_run_review", new=always_reject),
        patch.object(AdapterLlmDispatch, "_run_quality_gate") as mock_gate,
        patch.object(
            AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
        ) as mock_call,
    ):
        mock_gate.return_value = ModelQualityGateResult(
            ruff_pass=True, import_pass=True, test_pass=True
        )
        mock_call.return_value = _VALID_PYTHON
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=reviewer_ep,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=3,
            correlation_id=uuid.uuid4(),
        )

    assert code is None
    assert len(traces) == 3
    assert all(not t.accepted for t in traces)
    assert all(t.failure_kind == _FAILURE_REVIEW_REJECTED for t in traces)


# ---------------------------------------------------------------------------
# _generate_with_review: empty code is generation_malformed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_code_traced_as_generation_malformed(tmp_path: Path) -> None:
    """Empty code extraction produces a generation_malformed trace."""
    adapter = _make_adapter(tmp_path, max_attempts=1)

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = _EMPTY_RESPONSE
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=None,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=1,
            correlation_id=uuid.uuid4(),
        )

    assert code is None
    assert len(traces) == 1
    assert traces[0].failure_kind == _FAILURE_GENERATION_MALFORMED


# ---------------------------------------------------------------------------
# _generate_with_review: review_unavailable traced distinctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_unavailable_traced_distinctly(tmp_path: Path) -> None:
    """Reviewer returning None (unavailable) traces as review_unavailable, not approved."""
    adapter = _make_adapter(tmp_path, allow_unreviewed=False, max_attempts=1)
    reviewer_ep = _make_reviewer_endpoint()

    async def always_unavailable(
        self_: object = None, **kw: object
    ) -> tuple[str, None]:
        return "unavailable", None

    with (
        patch.object(AdapterLlmDispatch, "_run_review", new=always_unavailable),
        patch.object(
            AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
        ) as mock_call,
    ):
        mock_call.return_value = _VALID_PYTHON
        code, traces = await adapter._generate_with_review(
            target=_make_target(),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=reviewer_ep,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=1,
            correlation_id=uuid.uuid4(),
        )

    assert code is None
    assert len(traces) == 1
    assert traces[0].failure_kind == _FAILURE_REVIEW_UNAVAILABLE
    assert traces[0].accepted is False


# ---------------------------------------------------------------------------
# _generate_with_review: trace files written for every attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_attempts_produce_trace_files(tmp_path: Path) -> None:
    """Every attempt (pass or fail) produces a trace file in .onex_state/dispatch-traces/."""
    corr_id = uuid.uuid4()
    adapter = _make_adapter(tmp_path, max_attempts=3, allow_unreviewed=True)

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.side_effect = ["bad syntax @@@", "bad syntax @@@", _VALID_PYTHON]
        code, _traces = await adapter._generate_with_review(
            target=_make_target("OMN-TRACE"),
            coder_endpoint=_make_coder_endpoint(),
            reviewer_endpoint=None,
            template_source="",
            target_source="",
            model_sources=[],
            max_attempts=3,
            correlation_id=corr_id,
        )

    traces_dir = tmp_path / ".onex_state" / "dispatch-traces"
    assert traces_dir.exists()
    trace_files = list(traces_dir.glob(f"{corr_id}-OMN-TRACE-attempt-*.json"))
    assert len(trace_files) == 3
    assert code is not None


# ---------------------------------------------------------------------------
# handle(): end-to-end wiring with retry loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_wires_generate_with_review(tmp_path: Path) -> None:
    """handle() uses _generate_with_review; accepted code increments total_dispatched."""
    adapter = _make_adapter(tmp_path, allow_unreviewed=True)
    adapter._endpoints.pop(EnumModelTier.FRONTIER_REVIEW, None)  # type: ignore[union-attr]

    targets = (
        BuildTarget(ticket_id="OMN-A", title="Fix A", buildability="auto_buildable"),
        BuildTarget(ticket_id="OMN-B", title="Fix B", buildability="auto_buildable"),
    )

    with patch.object(
        AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = _VALID_PYTHON
        result = await adapter.handle(
            correlation_id=uuid.uuid4(),
            targets=targets,
        )

    assert result.total_dispatched == 2
    for payload in result.delegation_payloads:
        assert payload.payload["accepted"] is True
        assert payload.payload["total_attempts"] == 1


@pytest.mark.asyncio
async def test_handle_counts_only_accepted(tmp_path: Path) -> None:
    """handle() only increments total_dispatched for accepted tickets."""
    adapter = _make_adapter(tmp_path, allow_unreviewed=False, max_attempts=1)

    targets = (
        BuildTarget(ticket_id="OMN-C", title="Fix C", buildability="auto_buildable"),
    )

    async def always_unavailable_handle(
        self_: object = None, **kw: object
    ) -> tuple[str, None]:
        return "unavailable", None

    with (
        patch.object(AdapterLlmDispatch, "_run_review", new=always_unavailable_handle),
        patch.object(
            AdapterLlmDispatch, "_call_endpoint", new_callable=AsyncMock
        ) as mock_call,
    ):
        mock_call.return_value = _VALID_PYTHON
        result = await adapter.handle(
            correlation_id=uuid.uuid4(),
            targets=targets,
        )

    assert result.total_dispatched == 0
    assert result.delegation_payloads[0].payload["accepted"] is False
