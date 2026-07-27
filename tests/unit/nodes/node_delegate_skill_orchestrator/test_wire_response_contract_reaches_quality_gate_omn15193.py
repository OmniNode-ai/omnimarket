# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end wire-model response_contract reachability (OMN-15193/OMN-15196).

Drives the FULL seam a wire-level caller (e.g. steel's LlmBusDelegationClient,
OMN-15170) actually exercises -- the OMN-14208 cross-boundary discipline: one
seam test through the REAL handler + REAL dispatch port + REAL quality-gate
reducer, not two independent unit suites:

    ModelDelegateSkillRequest(task_type="agent_delegation", response_contract={...})
      -> HandlerDelegateSkill.handle()
      -> LocalDelegationDispatchPort.dispatch(response_contract=...)
      -> HandlerLlmDelegationCall (effect, injected) returns tactical JSON
      -> node_delegation_quality_gate_reducer.handlers.handler_quality_gate.delta()
         validates structurally against the declared (or task-class default) schema

This is the live-reproduced OMN-15193 defect (deterministic 6/6, see
omni_home/docs/evidence/2026-07-26-omn15170-live-driver.md): the
``agent_delegation`` task-class heuristics (``sub_tasks_verified`` substring
match + ``no_refusal`` phrase match) reject a well-formed tactical response
whose ``rationale`` field legitimately contains the substring "i cannot". A
declared ``response_contract`` makes the SAME response PASS.

OMN-15196 retires those heuristics entirely: ``agent_delegation`` now declares
a task-class DEFAULT response contract (``response_contract_ref`` ->
``omnibase_core.models.dispatch.report.DispatchReport``, OMN-15161) that
``LocalDelegationDispatchPort`` resolves and applies whenever the CALLER
supplies no ``response_contract`` of its own. So the tactical response with no
caller-declared contract still gets REJECTED, but now via SCHEMA_VIOLATION
against the class default, not the retired keyword heuristics; a caller (like
steel) that supplies its OWN schema still takes precedence over the class
default, unchanged from OMN-15193; and a response actually shaped like a
dispatch-worker report PASSES with no caller-declared contract at all.

Only ``delegation_backend_resolution.load_bifrost_backends`` is patched (to an
in-memory backends list pinned by ``backend_id``, avoiding a dependency on live
overlay/store secrets) -- ``resolve_delegation_backend``, ``LocalDelegationDispatchPort``,
``HandlerDelegateSkill``, and the real ``delta`` quality-gate reducer all run for
real, so this proves the actual schema-validation seam, not a mocked stand-in.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_LOCAL_CODER_MLX_ENDPOINT = "http://stickybeatz-studio:8401/v1/chat/completions"
_LOCAL_CODER_MLX_MODEL_ID = "mlx-community/Qwen3.6-35B-A3B-8bit"

# The steel tactical-decision response contract (OMN-15170/OMN-15193):
# {action, action_params, confidence, rationale}.
_TACTICAL_RESPONSE_CONTRACT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "action_params": {"type": "object"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["action", "action_params", "confidence", "rationale"],
}

# The live-reproduced defect payload: well-formed tactical JSON whose rationale
# legitimately contains "i cannot" as part of coherent prose, not a refusal.
_STEEL_SHAPED_RESPONSE_WITH_I_CANNOT_RATIONALE = json.dumps(
    {
        "action": "hold_position",
        "action_params": {"unit_id": "u-17"},
        "confidence": 0.82,
        "rationale": (
            "i cannot confirm the enemy flank is clear from current sensor "
            "coverage, so holding position is the lower-risk tactical choice."
        ),
    }
)


def _backends_with_local_coder_mlx() -> list[dict[str, Any]]:
    """A minimal in-memory bifrost backends list (mirrors OMN-15180's own
    fixture) with a resolvable local-coder-mlx endpoint."""
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://other.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        },
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": _LOCAL_CODER_MLX_ENDPOINT,
            "model_name": _LOCAL_CODER_MLX_MODEL_ID,
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["agent_delegation"],
        },
    ]


class _RecordingEffect:
    """Injected effect handler that records every call and returns fixed content."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _make_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, content: str
) -> tuple[HandlerDelegateSkill, _RecordingEffect]:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends_with_local_coder_mlx(),
    )
    effect = _RecordingEffect(content)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), effect


@pytest.mark.unit
async def test_declared_contract_passes_steel_shaped_response_with_i_cannot_rationale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-reproduced OMN-15193 defect fix: a declared response_contract
    makes a well-formed tactical response PASS even though its rationale
    legitimately contains the substring "i cannot" -- the exact false positive
    the ``no_refusal`` keyword heuristic produced (deterministic 6/6 repro)."""
    handler, effect = _make_handler(
        tmp_path,
        monkeypatch,
        content=_STEEL_SHAPED_RESPONSE_WITH_I_CANNOT_RATIONALE,
    )
    request = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_TACTICAL_RESPONSE_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.quality_gate_passed is True
    assert response.quality_gates_failed == []
    assert response.response == _STEEL_SHAPED_RESPONSE_WITH_I_CANNOT_RATIONALE
    assert len(effect.calls) == 1


@pytest.mark.unit
async def test_same_response_without_declared_contract_rejected_by_class_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-15196: the IDENTICAL response with NO CALLER-declared contract is
    still rejected -- but now via the ``agent_delegation`` task-class's OWN
    declared default (the per-role dispatch report contract, OMN-15161), not
    the retired ``sub_tasks_verified``/``no_refusal`` keyword heuristics. The
    steel-shaped tactical JSON (action/action_params/confidence/rationale)
    satisfies none of the four closed report shapes (implementer/verifier/
    lander/scout), so it fails SCHEMA_VIOLATION -- proving the class default is
    actually consulted, not merely declared and ignored."""
    handler, effect = _make_handler(
        tmp_path,
        monkeypatch,
        content=_STEEL_SHAPED_RESPONSE_WITH_I_CANNOT_RATIONALE,
    )
    request = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        # No response_contract declared -- resolves the agent_delegation
        # task-class default (response_contract_ref) instead.
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert any("SCHEMA_VIOLATION" in reason for reason in response.quality_gates_failed)
    assert not any("REFUSAL" in reason for reason in response.quality_gates_failed)
    # The local tier retries the SAME backend (OMN-14234 best-of-N) before
    # escalating; every retry sees the identical deterministic content and
    # fails identically, and escalation off "local" then exhausts the ladder
    # (no other tier's backend is resolvable in the patched bifrost config) --
    # every call was to the pinned backend, never a different one.
    assert len(effect.calls) >= 1
    assert all(call.provider == "local-coder-mlx" for call in effect.calls)


@pytest.mark.unit
async def test_task_class_default_contract_passes_a_real_dispatch_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-15196 RED/GREEN proof: a response shaped as a REAL dispatch-worker
    report (the per-role contract ``agent_delegation`` now declares, OMN-15161)
    PASSES the gate with no caller-declared response_contract at all -- the
    task-class default alone is sufficient acceptance authority. This is the
    positive case the retired ``sub_tasks_verified`` heuristic could only ever
    approximate by substring luck."""
    scout_report = json.dumps(
        {
            "role": "scout",
            "verdict": "found",
            "findings_paths": ["tests/unit/delegation/test_gap.py"],
            "summary": (
                "Investigated the reported coverage gap and confirmed a missing "
                "null-check regression test at the cited path."
            ),
        }
    )
    handler, effect = _make_handler(tmp_path, monkeypatch, content=scout_report)
    request = ModelDelegateSkillRequest(
        prompt="Investigate the reported gap",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        # No response_contract declared -- the scout report satisfies the
        # agent_delegation task-class's declared default (DispatchReport).
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.quality_gate_passed is True
    assert response.quality_gates_failed == []
    assert len(effect.calls) == 1


@pytest.mark.unit
async def test_declared_contract_mismatch_rejected_with_specific_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response that does NOT satisfy the declared schema is rejected with
    specific per-violation reasons (missing required field, wrong type) -- not
    a single opaque failure."""
    malformed_response = json.dumps(
        {
            "action": "hold_position",
            "confidence": "high",  # wrong type: should be number
            # action_params and rationale are both missing (required).
        }
    )
    handler, _effect = _make_handler(
        tmp_path,
        monkeypatch,
        content=malformed_response,
    )
    request = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_TACTICAL_RESPONSE_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.quality_gate_passed is False
    reasons = response.quality_gates_failed
    assert any(
        "SCHEMA_VIOLATION" in reason and "action_params" in reason for reason in reasons
    )
    assert any(
        "SCHEMA_VIOLATION" in reason and "rationale" in reason for reason in reasons
    )
    assert any(
        "SCHEMA_VIOLATION" in reason and "confidence" in reason for reason in reasons
    )


@pytest.mark.unit
async def test_declared_contract_rejects_empty_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty response fails response_contract validation with a specific
    MALFORMED reason, not a silent pass."""
    handler, _effect = _make_handler(tmp_path, monkeypatch, content="   ")
    request = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_TACTICAL_RESPONSE_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.quality_gate_passed is False
    assert any(
        "MALFORMED" in reason and "empty" in reason
        for reason in response.quality_gates_failed
    )


@pytest.mark.unit
async def test_declared_contract_rejects_non_json_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JSON response fails response_contract validation with a specific
    MALFORMED reason naming the JSON decode error."""
    handler, _effect = _make_handler(tmp_path, monkeypatch, content="not json at all")
    request = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        backend_id="local-coder-mlx",
        response_contract=_TACTICAL_RESPONSE_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.quality_gate_passed is False
    assert any(
        "MALFORMED" in reason and "not valid JSON" in reason
        for reason in response.quality_gates_failed
    )
