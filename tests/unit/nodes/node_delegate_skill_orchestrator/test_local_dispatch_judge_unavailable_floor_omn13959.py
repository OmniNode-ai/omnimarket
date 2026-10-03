# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local deterministic-floor acceptance and required-bar authority."""

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
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

# A good-but-mechanically-incomplete code answer: clears the code_generation
# deterministic floor (compiles / single artifact / non-empty) but carries none of
# the convention/regression heuristic markers, so the deterministic-only graded
# score is ~0.733 and fails the 0.85 bar WITHOUT the judge.
_GOOD_CODE = "### ANSWER\ndef add(a: int, b: int) -> int:\n    return a + b"

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
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


# ---------------------------------------------------------------------------
# End-to-end local dispatch: judge unavailable -> accept on deterministic floor
# ---------------------------------------------------------------------------


def test_valid_local_artifact_accepted_on_deterministic_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid local artifact completes on its deterministic floor without escalation."""
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_GOOD_CODE),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="code_generation")

    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    assert result["escalation_count"] == 0
    # Acceptance comes from the declared deterministic floor.
    assert float(result["quality_score"]) < 0.85


def test_deterministic_floor_still_rejects_empty_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared deterministic floor rejects an empty artifact."""
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(""),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
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
