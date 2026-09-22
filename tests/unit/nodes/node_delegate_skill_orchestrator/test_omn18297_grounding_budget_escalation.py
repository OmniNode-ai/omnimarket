# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-backend input budget escalates instead of truncating (OMN-18297).

Local delegation ``d715f096-27b9-444f-9355-3554819ef8a5`` answered a 57,120-
character prompt on a backend whose model reports a 131,072-token context
window. The prompt fit; the answer did not stay grounded in it. The budget
declared in the routing contract is therefore a GROUNDING bound, well below the
window, and a prompt above it must climb the ladder rather than be trimmed to
fit -- a trimmed prompt produces a confident answer to a different question.
"""

from __future__ import annotations

import asyncio
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

pytestmark = pytest.mark.unit

_LADDER: tuple[str, ...] = ("local", "cheap_cloud")
_TIER_BACKEND_ID: dict[str, str] = {
    "local": "local-heavy-reasoning",
    "cheap_cloud": "cloud-gemini-pro",
}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}
_TIER_MODEL: dict[str, str] = {
    "local": "Qwen3.6-35B-A3B",
    "cheap_cloud": "gemini-2.5-flash",
}

# The lab backend's declared budget, and a prompt measured above it. 8000 tokens
# is 32,000 characters in the estimator's units; the recorded prompt was 57,120.
_LOCAL_BUDGET = 8000
_OVER_BUDGET_PROMPT = "the window in question. " * 2400  # 57,600 chars
_UNDER_BUDGET_PROMPT = "summarise the window. " * 50

_GOOD_RESEARCH = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)


def _backend_for_tier(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_TIER_BACKEND_ID[tier],
        model_id=_TIER_MODEL[tier],
        endpoint_ref=f"https://{tier}.example/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


class _RecordingEffect:
    """Records the tier of every call the port actually makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_tier)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_RESEARCH,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _install_ladder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    budgets: dict[str, int | None],
    max_escalations: int = 2,
) -> None:
    def fake_resolve(task_type: str, *, backend_id: str | None = None):
        if backend_id is None:
            return _backend_for_tier("local")
        return _backend_for_tier(_BACKEND_TIER[backend_id])

    def fake_next_eligible_tier(
        current,
        excluded,
        *,
        task_type=None,
        roi_overlay=None,
        excluded_backend_refs=frozenset(),
    ):
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
        port_mod, "first_eligible_tier", lambda _task_type, **_kwargs: "local"
    )
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda tier, _task_type: _TIER_BACKEND_ID[tier]
    )
    monkeypatch.setattr(
        port_mod, "tier_for_backend", lambda backend_id: _BACKEND_TIER.get(backend_id)
    )
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _t: max_escalations
    )
    # No free-tier re-draft in this ladder: the budget hop is what is under test.
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(
        port_mod,
        "resolve_backend_grounding_budget",
        lambda backend_id: budgets.get(backend_id),
    )


def _dispatch(
    port: LocalDelegationDispatchPort, *, prompt: str, task_type: str = "research"
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt=prompt,
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


def test_over_budget_input_escalates_and_is_never_sent_to_the_local_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before OMN-18297: the oversized prompt was dispatched to local."""
    _install_ladder(
        monkeypatch,
        budgets={"local-heavy-reasoning": _LOCAL_BUDGET, "cloud-gemini-pro": None},
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, prompt=_OVER_BUDGET_PROMPT)

    assert effect.calls == ["cheap_cloud"], effect.calls
    assert result["status"] == "completed"
    assert result["escalation_count"] == 1


def test_receipt_records_the_budget_and_the_measured_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both numbers, as a pair, on the skipped rung's attempt record."""
    _install_ladder(
        monkeypatch,
        budgets={"local-heavy-reasoning": _LOCAL_BUDGET, "cloud-gemini-pro": None},
    )
    port = LocalDelegationDispatchPort(
        effect_handler=_RecordingEffect(),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, prompt=_OVER_BUDGET_PROMPT)

    attempts = result["attempts"]
    assert isinstance(attempts, list)
    skipped = attempts[0]
    assert skipped["backend_id"] == "local-heavy-reasoning"
    assert skipped["input_token_budget"] == _LOCAL_BUDGET
    assert skipped["input_tokens_measured"] == len(_OVER_BUDGET_PROMPT) // 4
    assert skipped["input_tokens_measured"] > _LOCAL_BUDGET
    assert (
        skipped["failure_class"] == EnumDelegationFailureClass.CONTEXT_TOO_LARGE.value
    )
    assert "grounding budget" in str(skipped["error_message"])
    assert "not the model's context window" in str(skipped["error_message"])


def test_under_budget_input_is_dispatched_to_the_local_backend_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path is byte-identical: no budget hop for an in-budget prompt."""
    _install_ladder(
        monkeypatch,
        budgets={"local-heavy-reasoning": _LOCAL_BUDGET, "cloud-gemini-pro": None},
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, prompt=_UNDER_BUDGET_PROMPT)

    assert effect.calls == ["local"]
    assert result["escalation_count"] == 0
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert all(a.get("input_token_budget") is None for a in attempts)


def test_a_backend_declaring_no_budget_is_unbounded_not_defaulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None means NOT DECLARED. No backend is silently given a budget."""
    _install_ladder(
        monkeypatch,
        budgets={"local-heavy-reasoning": None, "cloud-gemini-pro": None},
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    _dispatch(port, prompt=_OVER_BUDGET_PROMPT)

    assert effect.calls == ["local"]


def test_over_budget_with_no_reachable_rung_fails_rather_than_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every rung over budget is a terminal failure, never a silent trim."""
    _install_ladder(
        monkeypatch,
        budgets={
            "local-heavy-reasoning": _LOCAL_BUDGET,
            "cloud-gemini-pro": _LOCAL_BUDGET,
        },
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, prompt=_OVER_BUDGET_PROMPT)

    assert effect.calls == []
    assert result["status"] == "failed"
    failed = result["quality_gates_failed"]
    assert isinstance(failed, list)
    assert "grounding budget" in failed[0]


def test_lab_backends_declare_a_budget_in_the_shipped_contract() -> None:
    """Wiring proof: the budget is a routing-contract fact, not a test constant."""
    from pathlib import Path as _Path

    import yaml

    contract = (
        _Path(port_mod.__file__).parents[3] / "configs" / "bifrost_delegation.yaml"
    )
    backends = yaml.safe_load(contract.read_text())["backends"]
    declared = {
        b["backend_id"]: b.get("max_grounded_input_tokens")
        for b in backends
        if b["backend_id"]
        in {"local-coder", "local-heavy-reasoning", "local-ds-v4-flash"}
    }
    assert declared == {
        "local-coder": _LOCAL_BUDGET,
        "local-heavy-reasoning": _LOCAL_BUDGET,
        "local-ds-v4-flash": _LOCAL_BUDGET,
    }
