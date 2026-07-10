# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retry-local (best-of-N) on the bus-less CLI dispatch port [OMN-14234].

``onex delegate`` runs the in-process ``LocalDelegationDispatchPort`` loop. Live
evidence (2026-07-09, ``--task-type refactor``, 3 runs on the same trivial
repoint) showed the local coder scoring 0.8 / 0.64 / 1.0 at the 0.85 bar — only
~1/3 cleared it single-shot, and the other ~2/3 escalated to PAID GLM despite
local inference being $0.

OMN-14234 makes the port retry the SAME free-tier backend up to its
contract-declared ``max_retries`` budget BEFORE escalating off it. These tests
prove, with a deterministic in-memory ladder (the same style as
``test_local_dispatch_escalation_omn13849.py``) and a stateful effect that varies
its verdict per call:

  * a later local draft that clears the gate completes on local at $0
    (escalation_count 0);
  * only after the retry budget is exhausted does the loop escalate to the paid
    tier; and
  * a PAID tier is never retried (fail-closed).

``is_free_tier`` / ``tier_max_retries`` are monkeypatched in the port namespace so
the retry gate reflects this ladder (``local`` free with a 2-retry budget) rather
than live routing config.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

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

# Cheapest-first ladder: local (FREE) -> cheap_cloud (paid) -> claude (paid).
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
# local is FREE ($0); the paid tiers carry a metered cost so the escalation-cost
# assertion is non-trivial.
_TIER_COST: dict[str, Decimal] = {
    "local": Decimal("0"),
    "cheap_cloud": Decimal("0.010"),
    "claude": Decimal("0.030"),
}
_LOCAL_RETRY_BUDGET = 2

_GOOD_RESEARCH = (
    "According to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


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
    initial_tier: str = "local",
    max_escalations: int = 3,
) -> None:
    """Monkeypatch the routing authority the port imports to the in-memory ladder,
    with ``local`` a FREE tier carrying a ``_LOCAL_RETRY_BUDGET`` retry budget."""

    def fake_resolve(task_type: str, *, backend_id: str | None = None):
        if backend_id is None:
            return _backend_for_tier(initial_tier)
        return _backend_for_tier(_BACKEND_TIER[backend_id])

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
    monkeypatch.setattr(
        port_mod, "first_eligible_tier", lambda _task_type, **_kwargs: initial_tier
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
    # OMN-14234: local is the only free retry surface in this ladder, with a
    # 2-retry (best-of-3) budget; paid tiers get 0.
    monkeypatch.setattr(port_mod, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(
        port_mod,
        "tier_max_retries",
        lambda tier: _LOCAL_RETRY_BUDGET if tier == "local" else 0,
    )


class _SequencedEffect:
    """Effect returning a per-tier SEQUENCE of pass/fail verdicts across calls.

    ``sequences[tier][i]`` is whether the i-th call to ``tier`` should PASS (return
    good content) or FAIL (return a refusal). Once a tier's sequence is exhausted
    the last value repeats. Records every call's tier so a test can assert how many
    $0 local drafts ran before escalation.
    """

    def __init__(self, sequences: dict[str, list[bool]]) -> None:
        self._sequences = {k: list(v) for k, v in sequences.items()}
        self._idx: dict[str, int] = {}
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        self.calls.append(tier)
        seq = self._sequences.get(tier, [True])
        i = self._idx.get(tier, 0)
        passes = seq[min(i, len(seq) - 1)]
        self._idx[tier] = i + 1
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_RESEARCH if passes else _REFUSAL,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=_TIER_COST[tier],
            savings_usd=Decimal("0"),
        )


def _dispatch(port: LocalDelegationDispatchPort, cid: UUID) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=cid,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )


@pytest.mark.unit
def test_retry_local_passes_on_second_draft_at_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-of-N: local draft 1 fails, draft 2 passes -> completes on local, $0,
    escalation_count 0, exactly 2 local calls (no cloud escalation)."""
    _install_ladder(monkeypatch)
    effect = _SequencedEffect({"local": [False, True]})
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, uuid4())

    assert result["status"] == "completed"
    assert result["escalation_count"] == 0
    assert result["cost_usd"] == pytest.approx(0.0)
    assert effect.calls == ["local", "local"]


@pytest.mark.unit
def test_retry_local_exhausts_budget_then_escalates_to_paid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 local drafts (1 initial + 2 retries) all fail -> escalate to paid
    cheap_cloud, which passes. Cost = only the paid tier's spend (local is $0)."""
    _install_ladder(monkeypatch)
    effect = _SequencedEffect({"local": [False, False, False], "cheap_cloud": [True]})
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, uuid4())

    assert result["status"] == "completed"
    # Exactly one up-tier escalation (local -> cheap_cloud); the 2 retries did NOT
    # count as escalations.
    assert result["escalation_count"] == 1
    assert effect.calls == ["local", "local", "local", "cheap_cloud"]
    # local drafts are $0; only cheap_cloud's metered cost is banked.
    assert result["cost_usd"] == pytest.approx(float(Decimal("0.010")))


@pytest.mark.unit
def test_paid_tier_gate_fail_escalates_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: starting on a PAID tier, a sub-bar draft escalates immediately —
    the paid tier is never re-drafted."""
    _install_ladder(monkeypatch, initial_tier="cheap_cloud")
    effect = _SequencedEffect({"cheap_cloud": [False], "claude": [True]})
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, uuid4())

    assert result["status"] == "completed"
    assert result["escalation_count"] == 1
    # No re-draft of cheap_cloud: exactly one call per tier.
    assert effect.calls == ["cheap_cloud", "claude"]
