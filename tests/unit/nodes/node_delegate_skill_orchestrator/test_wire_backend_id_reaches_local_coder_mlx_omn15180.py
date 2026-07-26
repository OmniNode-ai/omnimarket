# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end wire-model backend_id pin reachability (OMN-15180).

Drives the FULL seam a wire-level caller (e.g. steel's LlmBusDelegationClient,
OMN-15159) actually exercises:

    ModelDelegateSkillRequest(backend_id="local-coder-mlx")
      -> HandlerDelegateSkill.handle()
      -> LocalDelegationDispatchPort.dispatch(backend_id=...)
      -> resolve_delegation_backend(task_type, backend_id="local-coder-mlx")
      -> HandlerLlmDelegationCall (effect) invoked with local-coder-mlx's model_id

Before OMN-15180, ``ModelDelegateSkillRequest`` had no ``backend_id`` field at
all, so this whole chain was unreachable from any wire-level caller even
though ``LocalDelegationDispatchPort.dispatch()`` already implemented the pin
(OMN-15156, merged omnimarket#1897). This test goes RED against the pre-fix
wire model (``backend_id`` is not a constructor kwarg -> ``ValidationError``)
and GREEN once the field + handler plumbing land.

Only ``delegation_backend_resolution.load_bifrost_backends`` is patched (to an
in-memory backends list, avoiding a dependency on live overlay/store secrets)
-- ``resolve_delegation_backend`` itself, ``LocalDelegationDispatchPort``, and
``HandlerDelegateSkill`` all run for real, so this proves the actual pin
resolution logic, not a mocked stand-in.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_GOOD_DOC = (
    "This documents the retry path in detail, covering the timeout budget, the "
    "backoff policy, and the failure modes an operator should expect to see."
)

_LOCAL_CODER_MLX_ENDPOINT = "http://stickybeatz-studio:8401/v1/chat/completions"
_LOCAL_CODER_MLX_MODEL_ID = "mlx-community/Qwen3.6-35B-A3B-8bit"


def _backends_with_local_coder_mlx() -> list[dict[str, Any]]:
    """A minimal in-memory bifrost backends list, mirroring the shape
    real bifrost_delegation.yaml + a stability-test overlay would produce for
    local-coder-mlx (OMN-15155) alongside its sibling local-coder."""
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://other.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        },
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": _LOCAL_CODER_MLX_ENDPOINT,
            "model_name": _LOCAL_CODER_MLX_MODEL_ID,
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation", "refactoring", "documentation"],
        },
    ]


class _RecordingEffect:
    """Injected effect handler that records every call and always passes the gate."""

    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_DOC,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


@pytest.mark.unit
async def test_wire_level_backend_id_pin_reaches_local_coder_mlx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full wire -> handler -> port -> routing-authority seam resolves
    EXACTLY the pinned backend, never the (also-eligible) local-coder."""
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends_with_local_coder_mlx(),
    )

    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    handler = HandlerDelegateSkill(dispatch_port=port)

    request = ModelDelegateSkillRequest(
        prompt="Document the retry path",
        task_type="document",
        source="claude-code",
        backend_id="local-coder-mlx",
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.provider == _LOCAL_CODER_MLX_ENDPOINT
    assert response.model_name == _LOCAL_CODER_MLX_MODEL_ID
    assert len(effect.calls) == 1
    assert effect.calls[0].model_id == _LOCAL_CODER_MLX_MODEL_ID
    assert effect.calls[0].endpoint_ref == _LOCAL_CODER_MLX_ENDPOINT
    assert effect.calls[0].provider == "local-coder-mlx"


@pytest.mark.unit
async def test_wire_level_backend_id_pin_bypasses_tier_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin bypasses cheapest-first tier_order selection entirely for the
    initial attempt -- ``first_eligible_tier`` must never be consulted when a
    wire-level pin is present (mirrors OMN-15156's own port-level assertion,
    driven here from the wire model instead of the port directly)."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port_mod,
    )

    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends_with_local_coder_mlx(),
    )

    def _tier_order_should_not_be_consulted(*_a: object, **_k: object) -> str:
        raise AssertionError(
            "first_eligible_tier was consulted despite a wire-level "
            "backend_id pin -- the pin must bypass tier_order selection"
        )

    monkeypatch.setattr(
        port_mod, "first_eligible_tier", _tier_order_should_not_be_consulted
    )

    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    handler = HandlerDelegateSkill(dispatch_port=port)

    request = ModelDelegateSkillRequest(
        prompt="Document the retry path",
        task_type="document",
        source="claude-code",
        backend_id="local-coder-mlx",
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    assert response.model_name == _LOCAL_CODER_MLX_MODEL_ID
