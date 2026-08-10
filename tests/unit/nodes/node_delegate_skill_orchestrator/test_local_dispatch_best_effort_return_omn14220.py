# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14220: a terminal FAILURE must return the best authored artifact, not
discard it.

gen-leaf-2 ran a real ``--task-type refactor``: the LOCAL model authored a correct
artifact (score 1.0) but the gate (falsely) rejected it, the ladder escalated, the
final cloud hop returned a transport failure (429), and the terminal payload came
back EMPTY — the caller received nothing despite perfect work. The rejected content
was logged (OMN-14004) but never surfaced on the terminal payload. This test proves
the terminal now returns the best (highest-scoring, non-empty) authored artifact
across all attempts even when every tier is rejected / the last hop transport-fails.
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

_LADDER: tuple[str, ...] = ("local", "cheap_cloud")
_TIER_BACKEND_ID: dict[str, str] = {"local": "local-coder", "cheap_cloud": "cloud-glm"}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}
_TIER_MODEL: dict[str, str] = {"local": "Qwen3.6-35B-A3B", "cheap_cloud": "glm-5.2"}

_LOCAL_ARTIFACT = "def render(w):\n    return w.name  # the correct authored artifact"


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


class _LocalGoodThenCloudEmptyEffect:
    """local authors a correct artifact; the cloud hop returns empty content.

    Reproduces the gen-leaf-2 shape: an earlier tier authored the real artifact
    but the final hop yields nothing usable, so the old return path (which took
    only the LAST attempt's content) surfaced an empty terminal payload.
    """

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        content = _LOCAL_ARTIFACT if request.model_tier == "local" else ""
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


@pytest.mark.unit
def test_terminal_failure_returns_best_authored_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ladder(monkeypatch)
    # Force every tier's gate verdict to REJECT so the ladder escalates off the
    # local artifact and reaches the failing cloud hop — isolating the return path.
    monkeypatch.setattr(
        LocalDelegationDispatchPort,
        "_is_quality_accepted",
        lambda *_args, **_kw: False,
    )
    port = LocalDelegationDispatchPort(
        effect_handler=_LocalGoodThenCloudEmptyEffect(),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    result = asyncio.run(
        port.dispatch(
            prompt="refactor the import under TYPE_CHECKING",
            task_type="refactor",
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

    assert result["status"] == "failed"
    # The load-bearing assertion: the correct local artifact is RETURNED, not
    # discarded, even though the terminal hop was a 429 with empty content.
    assert result["content"] == _LOCAL_ARTIFACT, (
        "terminal failure must return the best authored artifact, not the empty "
        f"content of the failing final hop; got {result.get('content')!r}"
    )
