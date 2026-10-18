# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The grounding input budget is declared per task class (OMN-20486).

The local rung refused any code-edit loop turn above 8,000 input tokens before
a model call, because the one budget value on the backend was set for an
identifier-grounding failure on a summary task. The packaged
per-task-class budget config gives a code-generation turn its own value, and
every other class keeps the backend's default from the routing contract.
"""

from __future__ import annotations

import asyncio
import importlib.resources
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
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
    "cheap_cloud": "cloud-gemini-2-5-flash",
}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}

# 4 characters per token in the grounding estimator's units.
_CODE_EDIT_PROMPT = "def f(x):\n    return x\n" * 3334  # ~20,000 tokens
_SUMMARY_PROMPT = "the window in question. " * 3042  # ~18,245 tokens
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)


@pytest.fixture
def packaged_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Bind the packaged routing contract explicitly, with no machine overlay."""
    resource = importlib.resources.files("omnimarket").joinpath(
        "configs/bifrost_delegation.yaml"
    )
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield contract
    finally:
        routing._config = None
        routing._load_bifrost_endpoints.cache_clear()


class _RecordingEffect:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_tier)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_ANSWER,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _backend_for_tier(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_TIER_BACKEND_ID[tier],
        model_id=f"model-{tier}",
        endpoint_ref=f"https://{tier}.example/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the ladder only; the budget is read from the real bound contract."""

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
        correlation_id=None,
    ):
        idx = _LADDER.index(current)
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
        port_mod,
        "backend_id_for_tier",
        lambda tier, _task_type, **_kw: _TIER_BACKEND_ID[tier],
    )
    monkeypatch.setattr(
        port_mod, "tier_for_backend", lambda backend_id: _BACKEND_TIER.get(backend_id)
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)


def _dispatch(
    tmp_path: Path, effect: _RecordingEffect, *, prompt: str, task_type: str
) -> dict[str, object]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
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


def test_code_edit_turn_of_20000_tokens_is_sent_to_the_local_rung_grounded_input_budget(
    packaged_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED on origin/dev: the one 8,000 token value refused this turn."""
    _install_ladder(monkeypatch)
    effect = _RecordingEffect()

    _dispatch(tmp_path, effect, prompt=_CODE_EDIT_PROMPT, task_type="code_generation")

    assert effect.calls[:1] == ["local"], effect.calls


def test_summary_prompt_above_the_summary_budget_still_escalates_grounded_input_budget(
    packaged_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    effect = _RecordingEffect()

    _dispatch(tmp_path, effect, prompt=_SUMMARY_PROMPT, task_type="research")

    assert effect.calls == ["cheap_cloud"], effect.calls


def test_budget_resolves_per_task_class_grounded_input_budget(
    packaged_base: Path,
) -> None:
    default = routing.resolve_backend_grounding_budget("local-heavy-reasoning")
    code = routing.resolve_backend_grounding_budget(
        "local-heavy-reasoning", "code_generation"
    )
    other = routing.resolve_backend_grounding_budget(
        "local-heavy-reasoning", "research"
    )

    assert default == 8000
    assert other == default
    assert code is not None
    assert code >= 20000


def test_code_class_budget_cites_a_measured_readback_grounded_input_budget() -> None:
    """The comment above each code-class value names a token count and a run id."""
    text = (
        importlib.resources.files("omnimarket")
        .joinpath("configs/grounding_budget_by_task_class.yaml")
        .read_text(encoding="utf-8")
    )
    declared = yaml.safe_load(text)["backends"]
    with_class = [b for b, classes in declared.items() if classes]
    assert with_class
    marker = "OMN-20486 measured readback:"
    assert text.count(marker) == len(with_class)
    for line in text.splitlines():
        if marker in line:
            assert "run_id" in line, line
            assert "tokens" in line, line


def test_bifrost_wire_contract_carries_no_class_budget_key_grounded_input_budget(
    packaged_base: Path,
) -> None:
    """The released bifrost wire model refuses unknown keys (OMN-18868)."""
    declared = yaml.safe_load(packaged_base.read_text(encoding="utf-8"))["backends"]
    assert all("max_grounded_input_tokens_by_task_class" not in b for b in declared)
