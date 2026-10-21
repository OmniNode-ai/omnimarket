# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A slow or rejected local rung hands off to the next rung (OMN-17427).

THE DEFECT, reported from 46 in-memory-bus runs on a laptop: the 240 s
delegation budget covers the whole run, not each rung, and the in-process
ladder gave every attempt its backend's own ``timeout_ms`` (300 s for the
local rungs). One unanswered local try, or a few slow rejected local drafts,
spent the run's whole budget, so the handler's deadline cancelled the run
before any cloud rung was tried. Run 6b81c964-4c34-45bc-b3f9-9283cf1e51ce
timed out on its first local attempt with nothing tried after it.

THE FIX, asserted here: each attempt keeps its backend's own contract
``timeout_ms`` as its per-rung timeout, bounded by what is left of the run's
budget minus the reserve the task-class contract keeps for the next rung. A
same-tier re-draft is issued only when a draft as long as the last one would
still leave that reserve.

The clock is a fake the port reads through ``_ladder_clock``, and the effect
child is replaced by a stand-in that advances it, so a 300 s timeout costs no
real time.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.inference.task_class_authority import (
    ModelTaskClassExecutionBudget,
    resolve_task_class_execution_budget,
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

pytestmark = pytest.mark.unit

_RUN_BUDGET_SECONDS = 240
_LADDER = ("local", "cheap_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-glm"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "Qwen3.6-35B-A3B", "cheap_cloud": "glm-5.2"}
# The local rungs declare 300 s in bifrost_delegation.yaml; the defect needs it.
_TIMEOUT_MS = {"local": 300000, "cheap_cloud": 60000}
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=_TIMEOUT_MS[tier],
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch, *, free_local: bool) -> None:
    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend("local" if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = _LADDER[_LADDER.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: "local")
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(
        port_mod, "is_free_tier", lambda tier: free_local and tier == "local"
    )
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 2)


def _answer(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id=request.request_id,
        success=True,
        content=_GOOD_ANSWER if request.model_tier == "cheap_cloud" else _REFUSAL,
        tokens_in=11,
        tokens_out=22,
        latency_ms=5,
        actual_cost_usd=Decimal("0"),
        savings_usd=Decimal("0"),
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    local_behaviour: str,
    local_seconds: float = 0.0,
    backend_id: str | None = None,
) -> tuple[dict[str, Any], _FakeClock, list[tuple[str, float]]]:
    """Run one delegation; return the result, the clock and each call's deadline."""
    clock = _FakeClock()
    calls: list[tuple[str, float]] = []

    async def effect_child(
        handler: Any, request: ModelLlmDelegationCallRequest, *, timeout_seconds: float
    ) -> ModelLlmDelegationCallResult:
        calls.append((request.model_tier, timeout_seconds))
        if request.model_tier == "local":
            if local_behaviour == "hang":
                clock.advance(timeout_seconds)
                raise TimeoutError
            clock.advance(local_seconds)
            return handler(request)
        clock.advance(5.0)
        return handler(request)

    # raising=False: before OMN-17427 the port read no clock of its own.
    monkeypatch.setattr(port_mod, "_ladder_clock", clock, raising=False)
    monkeypatch.setattr(
        port_mod, "_run_effect_handler_with_killable_timeout", effect_child
    )
    port = LocalDelegationDispatchPort(
        effect_handler=_answer,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=True,
    )
    started = clock.now
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=_RUN_BUDGET_SECONDS,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            backend_id=backend_id,
        )
    )
    clock.now -= started
    return result, clock, calls


def test_a_local_rung_that_times_out_reaches_the_next_rung_inside_the_budget(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, free_local=False)
    result, clock, calls = _run(monkeypatch, tmp_path, local_behaviour="hang")

    assert [tier for tier, _ in calls] == ["local", "cheap_cloud"]
    assert result["status"] == "completed", result
    assert [a["tier"] for a in result["attempts"]] == ["local", "cheap_cloud"]
    # The handler cancels the whole run at the budget; rung 2 must finish first.
    assert clock.now <= _RUN_BUDGET_SECONDS, calls


def test_the_first_rung_leaves_the_reserve_and_the_last_rung_gets_the_rest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, free_local=False)
    # OMN-21214's per-rung floor is a second, tighter bound on rung 1; this
    # test measures the OMN-17427 reserve alone, so the floor is switched off.
    monkeypatch.setattr(
        port_mod, "resolve_task_class_rung_floor_seconds", lambda _: None
    )
    _, _, calls = _run(monkeypatch, tmp_path, local_behaviour="hang")

    reserve = resolve_task_class_execution_budget("research").next_rung_reserve_seconds
    assert reserve > 0
    (_, local_deadline), (_, cloud_deadline) = calls
    assert local_deadline == _RUN_BUDGET_SECONDS - reserve
    # Rung 2 gets what rung 1 left, which is less than its own 60 s timeout_ms
    # plus the dispatch buffer, so the run budget is what binds it.
    assert cloud_deadline == _RUN_BUDGET_SECONDS - local_deadline


def test_a_pinned_run_gives_its_one_rung_the_whole_budget(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin takes no later rung, so it keeps no reserve for one."""
    _install_ladder(monkeypatch, free_local=False)
    _, _, calls = _run(
        monkeypatch, tmp_path, local_behaviour="hang", backend_id="local-coder"
    )

    assert calls == [("local", float(_RUN_BUDGET_SECONDS))]


def test_slow_rejected_local_drafts_stop_redrafting_and_hand_off(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, free_local=True)
    result, clock, calls = _run(
        monkeypatch, tmp_path, local_behaviour="reject", local_seconds=100.0
    )

    assert result["status"] == "completed", result
    assert [tier for tier, _ in calls][-1] == "cheap_cloud"
    # Three 100 s local drafts would spend 300 s; the run must stop re-drafting.
    assert clock.now <= _RUN_BUDGET_SECONDS, calls


def test_fast_rejected_local_drafts_still_get_their_free_redrafts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the reserve does not remove the $0 best-of-N retry."""
    _install_ladder(monkeypatch, free_local=True)
    result, _, calls = _run(
        monkeypatch, tmp_path, local_behaviour="reject", local_seconds=10.0
    )

    assert result["status"] == "completed", result
    assert [tier for tier, _ in calls] == ["local", "local", "local", "cheap_cloud"]


def test_every_task_class_declares_a_reserve_under_its_ceiling() -> None:
    from omnimarket.inference.task_class_authority import load_task_class_authority

    authority = load_task_class_authority()
    for task_class in authority.execution_budgets:
        budget = authority.execution_budget(task_class)
        assert budget.next_rung_reserve_seconds > 0
        assert (
            budget.next_rung_reserve_seconds < budget.task_class_timeout_ceiling_seconds
        )


def test_a_reserve_that_swallows_the_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="next_rung_reserve_seconds"):
        ModelTaskClassExecutionBudget(
            task_class_timeout_ceiling_seconds=60,
            terminal_delivery_margin_seconds=60,
            next_rung_reserve_seconds=60,
        )
