# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: a quality-refused local ladder names why it stopped climbing.

The unpinned hard-tier benchmark (paid escalation off) ended five cells with no
answer. Each walked local -> cheap_frontier and stopped, and the terminal the
local port returned carried ``status=failed`` with an EMPTY ``error_message``
and no ``terminal_failure_reason``: nothing on the receipt said whether the
escalation budget ran out, the next tier was paid, or the next rung resolved to
a route already tried. The bus orchestrator records that reason on the same
event (``_decide_escalation``); the in-process port, the path ``onex delegate``
runs, dropped it.

The fixture is the hard-tier shape: a free local tier, a paid tier the
paid-off run cannot use, then a free cloud tier. The ladder must reach the free
cloud tier past the paid one, and when that rung is refused too the terminal
must name its stop reason.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    NO_HIGHER_TIER_REASON_TOKEN,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

_LADDER: tuple[str, ...] = ("local", "cheap_cloud", "cheap_frontier", "claude")
_PAID: frozenset[str] = frozenset({"cheap_cloud", "claude"})
_TIER_BACKEND_ID: dict[str, str] = {
    "local": "local-heavy-reasoning",
    "cheap_cloud": "cloud-gemini-2-5-flash",
    "cheap_frontier": "openrouter-nemotron-ultra",
    "claude": "cloud-gemini-2-5-flash",
}
_TIER_MODEL: dict[str, str] = {
    "local": "Qwen3.8-27B",
    "cheap_cloud": "gemini-2.5-flash",
    "cheap_frontier": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "claude": "gemini-2.5-flash",
}


def _backend(
    tier: str, backend_id: str | None = None
) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id or _TIER_BACKEND_ID[tier],
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
    resolved_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Install the hard-tier ladder; return the recorded describe calls."""
    by_backend = {
        backend_id: tier
        for tier, backend_id in _TIER_BACKEND_ID.items()
        if tier not in _PAID
    }
    resolved_ids = resolved_ids or {}

    def fake_resolve(_task_type: str, *, backend_id: str | None = None):
        if backend_id is None:
            return _backend("local")
        tier = by_backend[backend_id]
        return _backend(tier, resolved_ids.get(backend_id))

    def fake_next_eligible_tier(
        current,
        excluded,
        *,
        task_type=None,
        roi_overlay=None,
        excluded_backend_refs=frozenset(),
        correlation_id=None,
    ):
        del task_type, roi_overlay, excluded_backend_refs, correlation_id
        idx = _LADDER.index(current)
        for tier in _LADDER[idx + 1 :]:
            if tier not in excluded and tier not in _PAID:
                return tier
        return None

    describe_calls: list[dict[str, Any]] = []

    def fake_describe(
        current_tier_name,
        excluded_tiers,
        *,
        task_type,
        excluded_backend_refs=frozenset(),
        correlation_id=None,
    ):
        describe_calls.append(
            {
                "current_tier": current_tier_name,
                "excluded_tiers": frozenset(excluded_tiers),
                "task_type": task_type,
            }
        )
        return (
            f"{NO_HIGHER_TIER_REASON_TOKEN}: task_class='{task_type}' "
            f"current_tier='{current_tier_name}'; no usable higher tier: "
            "claude(above_pricing_ceiling)"
        )

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", fake_resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", fake_next_eligible_tier)
    monkeypatch.setattr(
        port_mod,
        "first_eligible_tier",
        lambda _t, **_kw: "local",
    )
    monkeypatch.setattr(
        port_mod,
        "backend_id_for_tier",
        lambda tier, _task_type, **_kw: _TIER_BACKEND_ID[tier],
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: by_backend.get(b))
    monkeypatch.setattr(
        port_mod,
        "sibling_backend_available_in_tier",
        lambda _tier, _task_type, _excluded: None,
    )
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _t: max_escalations
    )
    monkeypatch.setattr(port_mod, "describe_no_higher_tier_available", fake_describe)
    monkeypatch.setattr(
        LocalDelegationDispatchPort,
        "_is_quality_accepted",
        lambda *_args, **_kw: False,
    )
    return describe_calls


class _AnswersEveryRungEffect:
    """Every rung answers; the forced gate verdict refuses each answer."""

    def __init__(self) -> None:
        self.tiers: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.tiers.append(str(request.model_tier))
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="### ANSWER\nthe review finds no defect in the hunk",
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _dispatch(tmp_path: Path, effect: _AnswersEveryRungEffect) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="review this diff hunk and name the defect",
            task_type="review",
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
    return result


@pytest.mark.unit
def test_hard_tier_reaches_free_cloud_tier_and_names_the_exhausted_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    describe_calls = _install_ladder(monkeypatch, max_escalations=3)
    effect = _AnswersEveryRungEffect()

    result = _dispatch(tmp_path, effect)

    # The paid tier is skipped, the free cloud tier is reached.
    assert effect.tiers == ["local", "cheap_frontier"]
    assert result["status"] == "failed"
    reason = result.get("terminal_failure_reason")
    assert isinstance(reason, str)
    assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN), reason
    assert describe_calls[-1]["current_tier"] == "cheap_frontier"
    assert describe_calls[-1]["task_type"] == "review"
    # The handler reads ``failure_reason`` into the receipt's ``error_message``.
    assert result.get("failure_reason") == reason


@pytest.mark.unit
def test_escalation_budget_spent_is_the_stop_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    describe_calls = _install_ladder(monkeypatch, max_escalations=1)
    effect = _AnswersEveryRungEffect()

    result = _dispatch(tmp_path, effect)

    assert effect.tiers == ["local", "cheap_frontier"]
    assert result["status"] == "failed"
    assert result.get("terminal_failure_reason") == "max_escalation_attempts_reached"
    assert result.get("failure_reason") == "max_escalation_attempts_reached"
    assert describe_calls == []


@pytest.mark.unit
def test_next_rung_resolving_to_a_tried_route_is_the_stop_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The benchmark host's shape: the free cloud rung became the customer route.

    Every house OpenRouter rung substitutes to the customer's one route, so a
    second climb onto that route is not a new responder (OMN-20157). The stop is
    correct; the terminal must say it was this, not leave the reason empty.
    """
    _install_ladder(
        monkeypatch,
        max_escalations=3,
        resolved_ids={"openrouter-nemotron-ultra": "local-heavy-reasoning"},
    )
    effect = _AnswersEveryRungEffect()

    result = _dispatch(tmp_path, effect)

    assert effect.tiers == ["local"]
    assert result["status"] == "failed"
    reason = result.get("terminal_failure_reason")
    assert isinstance(reason, str)
    assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN), reason
    assert "cheap_frontier" in reason
    assert "already-tried" in reason
