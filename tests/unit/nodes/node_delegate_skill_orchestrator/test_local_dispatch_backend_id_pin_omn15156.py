# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied ``backend_id`` pin tests for ``LocalDelegationDispatchPort`` (OMN-15156).

Hostile finding #4 on the steel-ONEX dispatch integration plan: ``dispatch()`` had
NO caller-supplied backend pin — resolution was purely ``task_type`` +
``tier_order`` via ``_resolve_initial_backend``. This adds an optional
``backend_id`` kwarg threaded ``dispatch()`` -> ``_resolve_initial_backend`` ->
``resolve_delegation_backend``'s pre-existing ``backend_id`` kwarg (the SAME
targeted-resolution path the LLM-judge already uses, OMN-13470).

Three behaviors under test:

  1. A pinned ``backend_id`` selects EXACTLY that backend and bypasses
     cheapest-first ``tier_order`` selection entirely for the initial attempt
     (``first_eligible_tier`` is never consulted).
  2. ``backend_id=None`` (the default) is byte-identical to pre-OMN-15156
     behavior — the tier-based ``first_eligible_tier`` -> ``backend_id_for_tier``
     -> ``resolve_delegation_backend`` chain still runs.
  3. Pinning an unknown/unresolvable ``backend_id`` fails LOUDLY — the real
     ``resolve_delegation_backend`` fail-closed ``RuntimeError`` propagates out of
     ``dispatch()`` verbatim, never silently falling back to a different backend
     and never invoking the effect handler.

Escalation-interaction note (P0 readback requirement, not redesigned here): if a
pinned backend's transport call fails, the existing escalation loop still
excludes the pinned backend's WHOLE TIER (via
``tier_for_backend(backend.backend_id)``) and re-resolves the next hop through
the normal closed-set ``tier_order`` — never back to the pin, never to a
different backend within the same tier. See
``_resolve_initial_backend.__doc__`` for the full note.
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
from omnimarket.routing import delegation_backend_resolution
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

_GOOD_RESEARCH = (
    "According to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)


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
            content=_GOOD_RESEARCH,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _dispatch(
    port: LocalDelegationDispatchPort,
    *,
    correlation_id: UUID,
    backend_id: str | None = None,
    task_type: str = "research",
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type=task_type,
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            backend_id=backend_id,
        )
    )


def test_pinned_backend_id_selects_exactly_that_backend_and_bypasses_tier_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied pin resolves the EXACT backend and never consults tiers.

    ``first_eligible_tier`` is monkeypatched to raise if called at all — proving
    the pin path bypasses cheapest-first tier_order selection entirely, not just
    happens to agree with it.
    """
    resolve_calls: list[tuple[str, str | None]] = []

    def _fake_resolve(
        task_type: str, *, backend_id: str | None = None, **_: object
    ) -> ModelResolvedDelegationBackend:
        resolve_calls.append((task_type, backend_id))
        assert backend_id == "cloud-gemini-pro"
        return ModelResolvedDelegationBackend(
            backend_id="cloud-gemini-pro",
            model_id="gemini-2.5-flash",
            endpoint_ref="https://claude.example/v1/chat/completions",
            tier="claude",
            max_tokens=4096,
            timeout_ms=30000,
        )

    def _tier_order_should_not_be_consulted(*_a: object, **_k: object) -> str:
        raise AssertionError(
            "first_eligible_tier was consulted despite a caller-supplied "
            "backend_id pin — the pin must bypass tier_order selection entirely"
        )

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", _fake_resolve)
    monkeypatch.setattr(
        port_mod, "first_eligible_tier", _tier_order_should_not_be_consulted
    )

    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(
        port,
        correlation_id=uuid4(),
        backend_id="cloud-gemini-pro",
        task_type="research",
    )

    assert resolve_calls == [("research", "cloud-gemini-pro")]
    assert result["status"] == "completed"
    assert result["model_name"] == "gemini-2.5-flash"
    assert result["delegated_to"] == "https://claude.example/v1/chat/completions"
    assert len(effect.calls) == 1
    assert effect.calls[0].model_id == "gemini-2.5-flash"


def test_backend_id_none_preserves_existing_tier_based_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: omitting the pin (``backend_id=None``) is byte-identical to the
    pre-OMN-15156 behavior — the tier-based chain still resolves the initial
    backend via ``first_eligible_tier`` -> ``backend_id_for_tier`` ->
    ``resolve_delegation_backend(task_type, backend_id=<tier-derived id>)``.
    """
    tier_calls: list[str] = []
    resolve_calls: list[tuple[str, str | None]] = []

    def _fake_first_eligible_tier(task_type: str, **_: object) -> str:
        tier_calls.append(task_type)
        return "local"

    def _fake_backend_id_for_tier(tier: str, _task_type: str) -> str:
        assert tier == "local"
        return "local-coder"

    def _fake_resolve(
        task_type: str, *, backend_id: str | None = None, **_: object
    ) -> ModelResolvedDelegationBackend:
        resolve_calls.append((task_type, backend_id))
        assert backend_id == "local-coder"
        return ModelResolvedDelegationBackend(
            backend_id="local-coder",
            model_id="Qwen3.6-35B-A3B",
            endpoint_ref="https://local.example/v1/chat/completions",
            tier="local",
            max_tokens=4096,
            timeout_ms=30000,
        )

    monkeypatch.setattr(port_mod, "first_eligible_tier", _fake_first_eligible_tier)
    monkeypatch.setattr(port_mod, "backend_id_for_tier", _fake_backend_id_for_tier)
    monkeypatch.setattr(port_mod, "resolve_delegation_backend", _fake_resolve)

    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    # Exercise both call shapes: the explicit default and a caller that omits the
    # (new, optional) kwarg entirely -- neither must change existing behavior.
    result_explicit_none = _dispatch(port, correlation_id=uuid4(), backend_id=None)
    result_omitted = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
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

    assert tier_calls == ["research", "research"]
    assert resolve_calls == [("research", "local-coder"), ("research", "local-coder")]
    for result in (result_explicit_none, result_omitted):
        assert result["status"] == "completed"
        assert result["model_name"] == "Qwen3.6-35B-A3B"
        assert result["delegated_to"] == "https://local.example/v1/chat/completions"


def test_unknown_backend_id_pin_fails_loudly_not_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning a backend_id absent from the routing config raises, never falls back.

    Uses the REAL ``resolve_delegation_backend`` (only ``load_bifrost_backends`` is
    patched to a small in-memory list) so this proves the real fail-closed
    contract, not a mocked stand-in: an unresolvable pin must not silently
    degrade to the untargeted/tier-based resolution, and the effect handler must
    never be invoked for a request that never resolved a backend.
    """
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: [
            {
                "backend_id": "local-coder",
                "endpoint_url": "http://inference.example:8000/v1/chat/completions",
                "model_name": "Qwen3.6-35B-A3B",
                "tier": "local",
                "max_tokens": 65536,
                "timeout_ms": 300000,
                "capabilities": ["research"],
            }
        ],
    )

    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )

    with pytest.raises(RuntimeError, match="does-not-exist"):
        _dispatch(port, correlation_id=uuid4(), backend_id="does-not-exist")

    assert effect.calls == []
