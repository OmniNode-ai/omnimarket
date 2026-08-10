# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14004: a rejected candidate's own content must be captured, not just its
failure reason.

Before this fix, ``LocalDelegationDispatchPort``'s escalation loop logged only
the gate's failure reasons on a quality-gate FAIL — the rejected candidate's raw
text was never written anywhere durable, so a false-reject (e.g. the
``compiles_without_errors`` language-assumption bug this same ticket fixes)
could not be debugged after the fact. This test proves the rejected content is
now logged at the escalation point, using the same monkeypatched-ladder harness
as the OMN-13849 escalation suite.
"""

from __future__ import annotations

import logging
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

_LADDER: tuple[str, ...] = ("local", "cheap_cloud")
_TIER_BACKEND_ID: dict[str, str] = {"local": "local-coder", "cheap_cloud": "cloud-glm"}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}
_TIER_MODEL: dict[str, str] = {"local": "Qwen3.6-35B-A3B", "cheap_cloud": "glm-5.2"}

_REJECTED_CANDIDATE_TEXT = "handler_routing: [this local answer got wrongly rejected"


def _backend_for_tier(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_TIER_BACKEND_ID[tier],
        model_id=_TIER_MODEL[tier],
        endpoint_ref=f"https://{tier}.example/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def fake_first_eligible_tier(_task_type, *, roi_overlay=None):
        del roi_overlay
        return "local"

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", fake_resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", fake_next_eligible_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", fake_first_eligible_tier)
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda tier, _task_type: _TIER_BACKEND_ID[tier]
    )
    monkeypatch.setattr(
        port_mod, "tier_for_backend", lambda backend_id: _BACKEND_TIER.get(backend_id)
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 1)


class _RejectThenPassEffect:
    """local tier returns a rejected candidate; cheap_cloud returns a good answer."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        content = (
            _REJECTED_CANDIDATE_TEXT
            if tier == "local"
            else "According to Smith (2020), the theorem in section 3 shows the "
            "tradeoff because the evidence supports it; therefore Y follows. "
            "See [12] for the methodical risk analysis."
        )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0.001"),
            savings_usd=Decimal("0"),
        )


@pytest.mark.unit
def test_rejected_candidate_content_is_logged_before_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    _install_ladder(monkeypatch)
    effect = _RejectThenPassEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    with caplog.at_level(logging.INFO, logger=port_mod.__name__):
        result = asyncio.run(
            port.dispatch(
                prompt="write a handler_routing block",
                task_type="research",
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

    assert result["status"] == "completed"
    assert result["escalation_count"] == 1

    rejected_content_logged = any(
        "rejected candidate content" in record.message
        and _REJECTED_CANDIDATE_TEXT in record.message
        for record in caplog.records
    )
    assert rejected_content_logged, (
        "expected the local tier's rejected candidate text to be logged before "
        "escalation (OMN-14004); only the failure reason was found instead"
    )
