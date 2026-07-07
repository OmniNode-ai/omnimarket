# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Escalation-loop + judge-combine tests for LocalDelegationDispatchPort (OMN-13849).

The bus-less local CLI delegation path was single-shot: one backend resolution,
one inference call, one quality-gate evaluation, terminal. OMN-13849 adds:

  1. An in-process escalation loop mirroring the bus orchestrator
     (``handler_delegation_workflow.handle_gate_result`` :1343-1400): on a
     quality-gate FAIL the port re-dispatches to the next eligible tier, bounded
     by ``escalation_policy.max_escalations`` from ``task_class_contracts.v1.yaml``.
  2. Judge-combine on the local path for ``JUDGE_COMBINABLE_TASK_TYPES``
     (``handler_quality_gate_intent.handle_async`` :127-155), reusing
     ``HandlerJudgeAdequacy`` so a good code answer clears the 0.85 bar.
  3. Cumulative metered cost across every attempt banked onto the evidence row.

The escalation ladder is driven through the routing-authority functions the port
imports (``resolve_delegation_backend`` / ``next_eligible_tier`` /
``backend_id_for_tier`` / ``resolve_task_class_max_escalations`` / ``tier_for_backend``),
monkeypatched in the port module namespace to a deterministic in-memory ladder —
so the loop logic is proven in isolation from live routing config. The effect
handler is injected to return a per-tier canned result (no network).

OMN-13943 supersedes this file's original "a hard transport/timeout failure is
always terminal" assumption: a RETRYABLE failure_class (e.g. RATE_LIMITED,
TIMEOUT, MODEL_UNAVAILABLE) now escalates through the SAME up-tier machinery a
quality-gate FAIL uses; only a non-retryable failure_class (PROVIDER_AUTH_FAILED,
INVALID_JSON) — or an exhausted/unreachable ladder — stays terminal. See the
retryable/non-retryable/bounded tests below.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
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

# --- Deterministic in-memory tier ladder ----------------------------------------
# Cheapest-first order: local -> cheap_cloud -> claude. Each tier maps to a
# resolvable backend with a COMPLETE endpoint and a metered cost. The port never
# reads real routing_tiers.yaml in these tests — the authority functions are
# monkeypatched below.

_LADDER: tuple[str, ...] = ("local", "cheap_cloud", "claude")
_TIER_BACKEND_ID: dict[str, str] = {
    "local": "local-coder",
    "cheap_cloud": "cloud-glm",
    "claude": "cloud-gemini-pro",
}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}
_TIER_MODEL: dict[str, str] = {
    "local": "Qwen3.6-35B-A3B",
    "cheap_cloud": "glm-5.2",
    "claude": "gemini-2.5-flash",
}
# Metered per-tier cost so the cumulative-cost assertions are non-trivial.
_TIER_COST: dict[str, Decimal] = {
    "local": Decimal("0.001"),
    "cheap_cloud": Decimal("0.010"),
    "claude": Decimal("0.030"),
}


def _backend_for_tier(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_TIER_BACKEND_ID[tier],
        model_id=_TIER_MODEL[tier],
        endpoint_ref=f"https://{tier}.example/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


def _install_ladder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_escalations: int,
    initial_tier: str = "local",
) -> None:
    """Monkeypatch the routing-authority functions the port imports.

    Models a closed-set cheapest-first ladder: the initial resolution returns
    ``initial_tier``; ``next_eligible_tier`` advances to the next un-excluded tier
    in ``_LADDER``; ``backend_id_for_tier`` maps a tier to its backend id; the
    per-backend re-resolution returns the ladder backend for that id.
    """

    def fake_resolve(task_type: str, *, backend_id: str | None = None):
        if backend_id is None:
            return _backend_for_tier(initial_tier)
        return _backend_for_tier(_BACKEND_TIER[backend_id])

    # ``roi_overlay`` accepted (OMN-14001) so the doubles match the extended
    # routing-authority signature; this ladder ignores it (no ROI suppression).
    def fake_next_eligible_tier(current, excluded, *, task_type=None, roi_overlay=None):
        try:
            idx = _LADDER.index(current)
        except ValueError:
            return None
        for tier in _LADDER[idx + 1 :]:
            if tier not in excluded:
                return tier
        return None

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", fake_resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", fake_next_eligible_tier)
    # OMN-13861: the INITIAL resolution now consults the closed-set tier_order via
    # first_eligible_tier (cheapest-first), exactly like every escalation hop. The
    # deterministic ladder's first tier is ``initial_tier``; backend_id_for_tier +
    # the backend_id-targeted fake_resolve then re-resolve it to the ladder backend.
    monkeypatch.setattr(
        port_mod,
        "first_eligible_tier",
        lambda _task_type, **_kwargs: initial_tier,
    )
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda tier, _task_type: _TIER_BACKEND_ID[tier]
    )
    monkeypatch.setattr(
        port_mod, "tier_for_backend", lambda backend_id: _BACKEND_TIER.get(backend_id)
    )
    monkeypatch.setattr(
        port_mod,
        "resolve_task_class_max_escalations",
        lambda _task_type: max_escalations,
    )


class _PerTierEffect:
    """Injected effect handler returning a canned result keyed by the model_tier.

    ``pass_tiers`` names the tiers whose returned content should pass the quality
    gate; all other tiers return a refusal (which fails the research DoD). Each
    tier's result carries its metered ``actual_cost_usd`` so cumulative-cost
    assertions are exercised. Records the tier of every call so the test can assert
    the escalation path.
    """

    def __init__(self, pass_tiers: frozenset[str]) -> None:
        self._pass_tiers = pass_tiers
        self.calls: list[str] = []

    _GOOD_RESEARCH = (
        "According to Smith (2020) and the theorem in section 3, the tradeoff is "
        "significant because the evidence shows X; therefore we conclude Y. See "
        "references [12] for the methodical analysis and the risk profile."
    )
    _REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        self.calls.append(tier)
        content = self._GOOD_RESEARCH if tier in self._pass_tiers else self._REFUSAL
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=_TIER_COST[tier],
            savings_usd=Decimal("0"),
        )


def _dispatch(
    port: LocalDelegationDispatchPort, *, task_type: str, correlation_id
) -> dict[str, object]:
    import asyncio

    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type=task_type,
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )


def test_gate_fail_on_tier_n_redispatches_tier_n_plus_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate FAIL on the local tier re-dispatches to cheap_cloud, which passes.

    The DoD mapping's core requirement: gate FAIL on tier N re-dispatches tier N+1
    per contract order. local returns a refusal (fails research DoD), cheap_cloud
    returns a good answer (passes) — the loop must escalate exactly once and
    terminate completed on cheap_cloud.
    """
    _install_ladder(monkeypatch, max_escalations=2)
    effect = _PerTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert effect.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    assert result["escalation_count"] == 1
    assert result["model_name"] == "glm-5.2"


def test_escalation_walks_full_ladder_in_contract_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two gate FAILs escalate local -> cheap_cloud -> claude (closed-set order)."""
    _install_ladder(monkeypatch, max_escalations=2)
    effect = _PerTierEffect(pass_tiers=frozenset({"claude"}))
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert effect.calls == ["local", "cheap_cloud", "claude"]
    assert result["status"] == "completed"
    assert result["escalation_count"] == 2
    assert result["model_name"] == "gemini-2.5-flash"


def test_escalation_bounded_by_max_escalations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With max_escalations=1 the loop stops after 1 up-tier re-dispatch.

    Every tier fails the gate. The bound must cap attempts at 1 + max_escalations
    = 2 total calls (local, cheap_cloud) and terminate FAILED without reaching
    claude — the contract escalation budget, not the ladder length, bounds the loop.
    """
    _install_ladder(monkeypatch, max_escalations=1)
    effect = _PerTierEffect(pass_tiers=frozenset())
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert effect.calls == ["local", "cheap_cloud"]
    assert result["status"] == "failed"
    assert result["quality_gate_passed"] is False
    assert result["escalation_count"] == 1


def test_ladder_exhaustion_terminates_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no higher tier exists, the loop terminates FAILED, not looping.

    max_escalations is generous (5) but the ladder only has 3 tiers; every tier
    fails the gate. The loop must stop at the ceiling tier (claude) — 3 total calls
    — because ``next_eligible_tier`` returns None past the ceiling.
    """
    _install_ladder(monkeypatch, max_escalations=5)
    effect = _PerTierEffect(pass_tiers=frozenset())
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert effect.calls == ["local", "cheap_cloud", "claude"]
    assert result["status"] == "failed"
    assert result["escalation_count"] == 2  # 2 up-tier hops across 3 tiers


def test_first_tier_pass_does_not_escalate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate PASS on the cheapest tier terminates immediately (no escalation)."""
    _install_ladder(monkeypatch, max_escalations=2)
    effect = _PerTierEffect(pass_tiers=frozenset({"local"}))
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert effect.calls == ["local"]
    assert result["status"] == "completed"
    assert result["escalation_count"] == 0


def test_cumulative_metered_cost_banked_across_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each attempt's real metered cost is summed into the projected evidence row.

    local ($0.001, rejected) + cheap_cloud ($0.010, rejected) + claude ($0.030,
    passed) -> the row's cost_usd is $0.041 — a rejected metered tier's spend is
    banked, never dropped, and never a hardcoded 0.0.
    """
    _install_ladder(monkeypatch, max_escalations=2)
    effect = _PerTierEffect(pass_tiers=frozenset({"claude"}))
    db_path = tmp_path / "d.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    correlation_id = uuid4()
    result = _dispatch(port, task_type="research", correlation_id=correlation_id)

    expected = float(Decimal("0.001") + Decimal("0.010") + Decimal("0.030"))
    assert result["cost_usd"] == pytest.approx(expected)
    # Per-attempt costs are surfaced for the CLI.
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert [a["cost_usd"] for a in attempts] == pytest.approx([0.001, 0.010, 0.030])

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert float(row["cost_usd"]) == pytest.approx(expected)
    assert bool(row["quality_gate_passed"]) is True


def test_retryable_transport_failure_on_tier_n_escalates_to_tier_n_plus_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-13943: a retryable transport failure (e.g. MODEL_UNAVAILABLE) now
    escalates to the next tier instead of terminating — superseding the OMN-13849
    "transport failure is always terminal" assumption this test previously
    asserted. This is exactly what makes a GLM RATE_LIMITED (429) transparently
    fall through to the next tier rather than terminating the whole delegation."""
    _install_ladder(monkeypatch, max_escalations=2)

    calls: list[str] = []

    def failing_then_ok_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        calls.append(tier)
        if tier == "local":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                error_message="connection refused",
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=(
                "According to Smith (2020) and the theorem in section 3, the "
                "tradeoff is significant because the evidence shows X; "
                "therefore we conclude Y. See references [12]."
            ),
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0.010"),
            savings_usd=Decimal("0"),
        )

    port = LocalDelegationDispatchPort(
        effect_handler=failing_then_ok_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert calls == ["local", "cheap_cloud"]  # escalated past the transport failure
    assert result["status"] == "completed"
    assert result["escalation_count"] == 1
    # OMN-14063: the skipped tier's WHY must be on its attempt record — this is
    # what makes a local->cloud escalation visible to the ModelDelegateSkillResponse
    # caller instead of only in the capture-file log.
    failed_attempt = result["attempts"][0]
    assert failed_attempt["tier"] == "local"
    assert failed_attempt["failure_class"] == "model_unavailable"
    assert failed_attempt["error_message"] == "connection refused"


def test_non_retryable_transport_failure_terminates_without_escalating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-retryable failure_class (PROVIDER_AUTH_FAILED) stays terminal.

    Re-issuing the same prompt against a different backend under different
    credentials will not turn a bad credential into a good one on the SAME
    backend, and escalating past an auth failure would mask a real
    credential-config bug as a transient one (OMN-13943)."""
    _install_ladder(monkeypatch, max_escalations=2)

    calls: list[str] = []

    def failing_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        calls.append(request.model_tier)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
            error_message="401 unauthorized",
        )

    port = LocalDelegationDispatchPort(
        effect_handler=failing_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert calls == ["local"]  # no escalation on a non-retryable failure_class
    assert result["status"] == "failed"
    assert result["escalation_count"] == 0


def test_retryable_transport_failure_on_every_tier_is_bounded_no_infinite_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable failure on EVERY tier still terminates — bounded by ladder
    exhaustion, never an infinite loop (OMN-13943 guardrail)."""
    _install_ladder(monkeypatch, max_escalations=5)

    calls: list[str] = []

    def always_rate_limited_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        calls.append(request.model_tier)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.RATE_LIMITED,
            error_message="429 rate limited",
        )

    port = LocalDelegationDispatchPort(
        effect_handler=always_rate_limited_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    # The deterministic ladder has exactly 3 tiers; the loop must terminate at
    # ladder exhaustion, not loop forever despite the generous max_escalations=5.
    assert calls == ["local", "cheap_cloud", "claude"]
    assert result["status"] == "failed"
    assert result["escalation_count"] == 2
