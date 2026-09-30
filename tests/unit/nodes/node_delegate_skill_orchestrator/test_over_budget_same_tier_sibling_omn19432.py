# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: an over-budget prompt tries the tier's other backends before the cloud.

OMN-18297 keeps a prompt above a backend's declared grounding budget off that
backend and climbs. The climb excluded the WHOLE tier, so a same-tier sibling
that holds the input was never asked: on 2026-09-30 four long ``review`` prompts
(17,111 to 22,128 tokens) went local-coder, refused at its 8000-token budget,
straight to a metered cloud rung, while the wide-window local backend sat idle.

What must hold, and what each test pins:

* the sibling that declares no budget takes the prompt, and the metered tier is
  never called;
* a sibling whose own budget the input also exceeds is skipped without an
  attempt, and is not offered again;
* with no sibling left the climb is what it was (positive control: the tier is
  excluded and the cloud rung answers);
* an in-budget prompt never leaves the rung it was routed to.
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
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = pytest.mark.unit

_SMALL_A = "local-coder"
_SMALL_B = "local-heavy-reasoning"
_WIDE = "local-studio-planner"
_CLOUD = "cloud-glm"
_BUDGET = 8000
_TIER = {_SMALL_A: "local", _SMALL_B: "local", _WIDE: "local", _CLOUD: "cheap_cloud"}
_MODEL = {
    _SMALL_A: "Qwen3.8-27B",
    _SMALL_B: "Qwen3.8-27B",
    _WIDE: "gpt-oss-120b",
    _CLOUD: "glm-5.3-flash",
}
_OVER = "the window in question. " * 2400  # 57,600 chars, 14,400 tokens
_UNDER = "summarise the window. " * 50

_GOOD = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the tradeoff "
    "is significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)


def _backend(backend_id: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id,
        model_id=_MODEL[backend_id],
        endpoint_ref=f"https://{backend_id}.example/v1/chat/completions",
        tier=_TIER[backend_id],
        max_tokens=4096,
        timeout_ms=30000,
    )


class _RecordingEffect:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.provider or "")
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    budgets: dict[str, int | None],
    local_order: tuple[str, ...] = (_SMALL_A, _SMALL_B, _WIDE),
) -> None:
    def fake_resolve(
        task_type: str, *, backend_id: str | None = None
    ) -> ModelResolvedDelegationBackend:
        return _backend(backend_id or local_order[0])

    def fake_sibling(
        tier_name: str, task_type: str, excluded: frozenset[str]
    ) -> str | None:
        if tier_name != "local":
            return None
        return next((b for b in local_order if b not in excluded), None)

    def fake_next_eligible_tier(
        current: str,
        excluded: frozenset[str],
        *,
        task_type: str | None = None,
        roi_overlay: object = None,
        excluded_backend_refs: frozenset[str] = frozenset(),
    ) -> str | None:
        return (
            None if "cheap_cloud" in excluded or current != "local" else "cheap_cloud"
        )

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", fake_resolve)
    monkeypatch.setattr(port_mod, "sibling_backend_available_in_tier", fake_sibling)
    monkeypatch.setattr(port_mod, "next_eligible_tier", fake_next_eligible_tier)
    monkeypatch.setattr(
        port_mod, "first_eligible_tier", lambda _task_type, **_kwargs: "local"
    )
    monkeypatch.setattr(
        port_mod,
        "backend_id_for_tier",
        lambda tier, _task_type, **_kw: local_order[0] if tier == "local" else _CLOUD,
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(
        port_mod, "resolve_backend_grounding_budget", lambda b: budgets.get(b)
    )


def _dispatch(port: LocalDelegationDispatchPort, prompt: str) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt=prompt,
            task_type="research",
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


def _port(tmp_path: Path, effect: _RecordingEffect) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )


def test_an_over_budget_prompt_goes_to_the_sibling_that_declares_no_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        budgets={_SMALL_A: _BUDGET, _SMALL_B: _BUDGET, _WIDE: None, _CLOUD: None},
    )
    effect = _RecordingEffect()

    result = _dispatch(_port(tmp_path, effect), _OVER)

    assert effect.calls == [_WIDE], effect.calls
    assert result["status"] == "completed"
    # A sideways hop inside the tier is not an escalation.
    assert result["escalation_count"] == 0
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    # The refused rung is on the receipt; the skipped over-budget sibling is not.
    assert [a["backend_id"] for a in attempts] == [_SMALL_A, _WIDE]


def test_a_sibling_whose_own_budget_is_exceeded_is_skipped_without_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        budgets={_SMALL_A: _BUDGET, _SMALL_B: _BUDGET, _WIDE: 30000, _CLOUD: None},
    )
    effect = _RecordingEffect()

    _dispatch(_port(tmp_path, effect), _OVER)

    assert effect.calls == [_WIDE]


def test_with_no_sibling_holding_the_input_the_climb_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL: every local budget exceeded, so the cloud answers."""
    _install(
        monkeypatch,
        budgets={_SMALL_A: _BUDGET, _SMALL_B: _BUDGET, _WIDE: 1000, _CLOUD: None},
    )
    effect = _RecordingEffect()

    result = _dispatch(_port(tmp_path, effect), _OVER)

    assert effect.calls == [_CLOUD]
    assert result["escalation_count"] == 1


def test_an_in_budget_prompt_never_leaves_the_rung_it_was_routed_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        budgets={_SMALL_A: _BUDGET, _SMALL_B: _BUDGET, _WIDE: None, _CLOUD: None},
    )
    effect = _RecordingEffect()

    result = _dispatch(_port(tmp_path, effect), _UNDER)

    assert effect.calls == [_SMALL_A]
    assert result["escalation_count"] == 0
