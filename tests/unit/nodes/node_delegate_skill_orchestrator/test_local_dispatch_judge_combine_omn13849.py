# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge-combine tests for LocalDelegationDispatchPort (OMN-13849).

The bus-less local path ran the quality gate deterministic-only, so a good
``code_generation`` answer (graded ~0.733) was structurally biased to FAIL the
0.85 bar regardless of tier — the judge combine that lifts a good code answer over
the bar ran only on the bus quality-gate-intent handler. OMN-13849 threads the
SAME ``HandlerJudgeAdequacy`` EFFECT into the local gate for
``JUDGE_COMBINABLE_TASK_TYPES``.

These tests inject a real ``HandlerJudgeAdequacy`` with a hermetic canned
inference bridge (``CannedAdequacyBridge`` — a fixed adequacy score, pinned to the
concrete resolved model id so a tier name can never reach the inference layer) so
the combine is exercised without a network call:

  * a passing judge (0.95) lifts a bare code answer (0.733 deterministic-only) over
    the 0.85 bar -> accepted;
  * a failing judge score (0.55 -> FAIL verdict) VETOES acceptance even though the
    deterministic floor passed (OMN-13642 veto parity);
  * a non-combinable task class never invokes the judge (the bridge records zero
    calls).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from tests.fixtures.judge_inference import CannedAdequacyBridge

# A good-but-mechanically-incomplete code answer: it clears the code_generation
# deterministic floor (compiles / single artifact / non-empty) but carries none of
# the convention/regression heuristic markers, so the deterministic-only graded
# score is ~0.733 and fails the 0.85 bar WITHOUT the judge.
_GOOD_CODE = "def add(a: int, b: int) -> int:\n    return a + b"

_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.6-35B-A3B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


def _no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the initial backend and make the ladder single-tier (no escalation).

    Isolates the judge-combine decision from the escalation loop: the initial
    resolution returns the local backend, ``next_eligible_tier`` always returns
    None (ceiling), and max_escalations is 0.
    """
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _LOCAL_BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )


def _code_effect(
    request: ModelLlmDelegationCallRequest,
) -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id=request.request_id,
        success=True,
        content=_GOOD_CODE,
        tokens_in=10,
        tokens_out=20,
        latency_ms=5,
        actual_cost_usd=Decimal("0.001"),
        savings_usd=Decimal("0"),
    )


def _dispatch(
    port: LocalDelegationDispatchPort, *, task_type: str
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="add two ints",
            task_type=task_type,
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )


def test_passing_judge_lifts_code_answer_over_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0.95 judge score combines to lift a 0.733 code answer over the 0.85 bar."""
    _no_escalation(monkeypatch)
    bridge = CannedAdequacyBridge(adequacy_score=0.95)
    port = LocalDelegationDispatchPort(
        effect_handler=_code_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    # The combined score is recorded and clears the bar (> the deterministic-only
    # 0.733 that would have failed).
    assert float(result["quality_score"]) >= 0.85
    # The judge really ran (one inference call).
    assert len(bridge.calls) == 1


def test_judge_unreachable_accepts_on_deterministic_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-13959 supersedes OMN-13849's "judge-down => fail" control.

    A judge whose inference errors (unresolvable / 429) yields JUDGE_FAILED -> no
    score -> deterministic-only 0.733 < 0.85. OMN-13849 originally documented this
    as "not accepted" (the structural bias the passing-judge combine fixes). But a
    valid LOCAL artifact must not require a reachable CLOUD judge to be accepted:
    OMN-13959 falls back to the deterministic FLOOR verdict when the judge is
    UNAVAILABLE (distinct from a reachable judge scoring the answer low, which the
    combined bar still rejects — see ``test_failing_judge_verdict_vetoes_acceptance``).
    The same code answer therefore now COMPLETES on the local tier.
    """
    _no_escalation(monkeypatch)

    class _FailingBridge:
        def resolved_model_id(self) -> str:
            return "glm-5.2"

        async def infer(self, *_a: object, **_k: object) -> str:
            raise RuntimeError("judge endpoint unreachable")

    port = LocalDelegationDispatchPort(
        effect_handler=_code_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=_FailingBridge()),  # type: ignore[arg-type]
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    # Accepted on the deterministic floor, not by clearing the combined bar.
    assert float(result["quality_score"]) < 0.85


def test_failing_judge_verdict_vetoes_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAIL judge verdict (0.55) vetoes acceptance even past the deterministic floor.

    OMN-13642 parity: the judge verdict is a co-required acceptance authority. A
    0.55 score maps to a FAIL verdict; the gate reducer vetoes acceptance and the
    port records the delegation FAILED.
    """
    _no_escalation(monkeypatch)
    bridge = CannedAdequacyBridge(adequacy_score=0.55)
    port = LocalDelegationDispatchPort(
        effect_handler=_code_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "failed"
    assert result["quality_gate_passed"] is False
    assert len(bridge.calls) == 1


def test_non_combinable_task_type_never_invokes_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-combinable class (research) does not call the judge at all."""
    _no_escalation(monkeypatch)
    bridge = CannedAdequacyBridge(adequacy_score=0.95)

    good_research = (
        "According to Smith (2020) and the theorem in section 3, the tradeoff is "
        "significant because the evidence shows X; therefore we conclude Y. See "
        "references [12] for the methodical analysis and the risk profile."
    )

    def research_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=good_research,
            tokens_in=10,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0.001"),
            savings_usd=Decimal("0"),
        )

    port = LocalDelegationDispatchPort(
        effect_handler=research_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),
    )
    result = _dispatch(port, task_type="research")

    assert result["status"] == "completed"
    # research is NOT in JUDGE_COMBINABLE_TASK_TYPES -> the judge is never called.
    assert bridge.calls == []
