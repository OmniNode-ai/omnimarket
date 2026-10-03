# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local acceptance and projection use the deterministic floor (OMN-20164)."""

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

# A good-but-mechanically-incomplete code answer: it clears the code_generation
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
    # OMN-14234: isolate the judge-combine decision from retry-local (best-of-N on a
    # free tier). Disable the free-tier retry gate so a single deterministic draft is
    # evaluated once — retry-local is proven separately in
    # ``test_local_dispatch_retry_local_omn14234.py``.
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)


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
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


def test_code_answer_accepted_on_deterministic_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared deterministic floor accepts and projects a valid artifact."""
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_code_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="code_generation")
    assert result["status"] == "completed"
    assert result["quality_gate_passed"] is True
    assert float(result["quality_score"]) < 0.85


def test_research_acceptance_and_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Research still uses its declared deterministic checks."""
    _no_escalation(monkeypatch)

    good_research = (
        "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the tradeoff is "
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
    )
    result = _dispatch(port, task_type="research")

    assert result["status"] == "completed"
