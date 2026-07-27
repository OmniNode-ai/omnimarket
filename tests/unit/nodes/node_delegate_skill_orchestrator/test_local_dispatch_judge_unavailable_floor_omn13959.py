# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge-unavailable deterministic-floor acceptance for the local port (OMN-13959).

The bus-less local ``onex delegate`` path runs the LLM-judge adequacy EFFECT for
``JUDGE_COMBINABLE_TASK_TYPES`` (OMN-13849) and applies the task-class
``required_bar`` (OMN-13849 ``_is_quality_accepted``). When the cloud judge is
unreachable / throttled (a 429 → ``JUDGE_FAILED`` verdict) the gate falls back to
the deterministic-only graded score, which for ``code_generation`` tops out ~0.733
— structurally below the 0.85 combined-score bar because the judge's 0.4
semantic-adequacy band is absent. Before OMN-13959 that rejected a VALID local
artifact and escalated it to ladder exhaustion during a cloud-judge outage,
defeating local-first.

OMN-13959: when the judge is unavailable (``score_source=deterministic_acceptance``
for a verifiable class, floor passed), acceptance falls back to the deterministic
FLOOR verdict instead of the un-meetable combined bar. This never accepts a
deterministic-floor rejection (empty / refusal / non-compiling), and never fires
when the judge IS reachable (``score_source=combined``, full bar applies).

The judge is exercised via the REAL ``HandlerJudgeAdequacy`` with an injected
``_UnavailableJudgeBridge`` whose ``infer`` raises — the exact
``JUDGE_LLM_CALL_FAILED`` path a live 429 drives — so no network call is made.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_COMBINED,
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
)
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

# A good-but-mechanically-incomplete code answer: clears the code_generation
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


class _UnavailableJudgeBridge:
    """A judge inference bridge whose ``infer`` raises — simulates a 429/outage.

    ``HandlerJudgeAdequacy.score`` catches the raise and returns a ``JUDGE_FAILED``
    verdict with ``actual_score=None`` (the ``JUDGE_LLM_CALL_FAILED`` path), exactly
    as a live z.ai GLM 429 does. ``resolved_model_id`` returns a concrete id so the
    judge records honest provenance (a tier name must never reach the inference
    layer).
    """

    def __init__(self) -> None:
        self.calls = 0

    def resolved_model_id(self) -> str:
        return "glm-5.2"

    async def infer(self, *_args: object, **_kwargs: object) -> str:
        self.calls += 1
        raise RuntimeError(
            "Client error '429 Too Many Requests' for url 'https://api.z.ai/...'"
        )


def _no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the initial backend and make the ladder single-tier (no escalation).

    Isolates the acceptance decision from the escalation loop: initial resolution
    returns the local backend, ``next_eligible_tier`` always returns None, and
    ``max_escalations`` is 0 — so a NON-accepted result terminates FAILED at the
    local tier instead of walking the ladder (which is what the bug did).
    """
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _LOCAL_BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )


def _effect_returning(content: str) -> port_mod._EffectHandler:
    def _effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=10,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    return _effect


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
            tenant_id=None,
        )
    )


# ---------------------------------------------------------------------------
# End-to-end local dispatch: judge unavailable -> accept on deterministic floor
# ---------------------------------------------------------------------------


def test_judge_unavailable_accepts_valid_local_artifact_on_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge 429 accepts a valid LOCAL code artifact on the deterministic floor.

    This is the OMN-13959 fix: the deterministic-only score (~0.733) is below the
    0.85 combined bar, but because the judge is UNAVAILABLE (not merely low) the
    port falls back to the deterministic-floor verdict and completes on the local
    tier — no escalation, quality gate passed.
    """
    _no_escalation(monkeypatch)
    bridge = _UnavailableJudgeBridge()
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_GOOD_CODE),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),  # type: ignore[arg-type]
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    assert result["escalation_count"] == 0
    # The judge really was attempted (and failed closed) — the deterministic floor,
    # not a phantom judge pass, carried acceptance.
    assert bridge.calls == 1
    # The recorded score is the deterministic-only graded score, below the bar; the
    # acceptance came from the floor fallback, not from clearing the bar.
    assert float(result["quality_score"]) < 0.85


def test_judge_unavailable_still_rejects_empty_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guardrail: the floor fallback never accepts a deterministic-floor rejection.

    An EMPTY code answer fails the deterministic empty floor
    (``fail_category=fail_deterministic``) even while the judge is unavailable, so
    the delegation is NOT accepted — the fallback is to the real DoD floor, never
    to "accept anything."
    """
    _no_escalation(monkeypatch)
    bridge = _UnavailableJudgeBridge()
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(""),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),  # type: ignore[arg-type]
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "failed"
    assert result["quality_gate_passed"] is False


def test_judge_reachable_low_score_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guardrail: when the judge IS reachable, the combined bar still applies.

    A reachable judge that scores the answer low (0.1 -> combined ~0.64) does NOT
    get the floor fallback (``score_source=combined``), so the 0.85 bar rejects it.
    The fix must not weaken the bar for the judge-available path.
    """
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_GOOD_CODE),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.1)
        ),
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "failed"
    assert result["quality_gate_passed"] is False


# ---------------------------------------------------------------------------
# _is_quality_accepted unit matrix
# ---------------------------------------------------------------------------


def _gate_result(
    *, passed: bool, score: float, score_source: str, fail_category: str
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=passed,
        fail_category=fail_category,  # type: ignore[arg-type]
        quality_score=score,
        score_source=score_source,
    )


@pytest.mark.unit
class TestIsQualityAcceptedJudgeUnavailable:
    def _port(self) -> LocalDelegationDispatchPort:
        return LocalDelegationDispatchPort.__new__(LocalDelegationDispatchPort)

    def test_judge_unavailable_floor_accepts_below_bar(self) -> None:
        """deterministic_acceptance + passed below the bar -> accepted (floor)."""
        r = _gate_result(
            passed=True,
            score=0.733,
            score_source=SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
            fail_category="pass",
        )
        assert self._port()._is_quality_accepted("code_generation", r) is True

    def test_judge_combined_below_bar_rejected(self) -> None:
        """combined + score below the bar -> rejected (bar preserved)."""
        r = _gate_result(
            passed=True,
            score=0.64,
            score_source=SCORE_SOURCE_COMBINED,
            fail_category="pass",
        )
        assert self._port()._is_quality_accepted("code_generation", r) is False

    def test_judge_combined_above_bar_accepted(self) -> None:
        """combined + score at/above the bar -> accepted."""
        r = _gate_result(
            passed=True,
            score=0.98,
            score_source=SCORE_SOURCE_COMBINED,
            fail_category="pass",
        )
        assert self._port()._is_quality_accepted("code_generation", r) is True

    def test_deterministic_floor_rejection_never_accepted(self) -> None:
        """fail_deterministic is refused even with the deterministic score_source."""
        r = _gate_result(
            passed=False,
            score=0.5,
            score_source=SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
            fail_category="fail_deterministic",
        )
        assert self._port()._is_quality_accepted("code_generation", r) is False
