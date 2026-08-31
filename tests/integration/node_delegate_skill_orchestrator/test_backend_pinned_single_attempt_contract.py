# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public/raw contract integration for pinned dispatch refusal."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_dispatch_policy import (
    canonical_first_effect_authorization_binding_type,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)


class _RejectedPinnedDispatchPort:
    """Tripwire proving the public handler refuses before its dispatch boundary."""

    def __init__(self) -> None:
        self.dispatch_kwargs: dict[str, object] | None = None

    async def dispatch(self, **kwargs: object) -> dict[str, object]:
        self.dispatch_kwargs = kwargs
        raise AssertionError("raw pinned request reached the dispatch boundary")


@pytest.mark.integration
async def test_raw_pinned_single_attempt_refuses_before_dispatch() -> None:
    correlation_id = uuid4()
    dispatch_port = _RejectedPinnedDispatchPort()
    request = ModelDelegateSkillRequest(
        prompt="Constrained lab task",
        task_type="research",
        source="codex",
        correlation_id=correlation_id,
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="d" * 64,
    )

    terminal = await HandlerDelegateSkill(dispatch_port=dispatch_port).handle(request)

    assert terminal.status == "failed"
    assert "raw request tenant_id is non-authorizing" in terminal.error_message
    assert dispatch_port.dispatch_kwargs is None


@pytest.mark.integration
@pytest.mark.skipif(
    canonical_first_effect_authorization_binding_type() is None,
    reason="requires Core ModelDelegationFirstEffectAuthorizationBinding",
)
async def test_rehydrated_core_authorization_cannot_enter_public_raw_path() -> None:
    """A caller-rehydrated DTO is rejected before the public handler boundary."""
    binding_type = canonical_first_effect_authorization_binding_type()
    assert binding_type is not None
    correlation_id = uuid4()
    rehydrated = binding_type(
        correlation_id=correlation_id,
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        rendered_contract_sha256="d" * 64,
        authorization_digest="a" * 64,
        grant_id=uuid4(),
        envelope_id=uuid4(),
        issuer_key_fingerprint_sha256="e" * 64,
        nonce_digest="b" * 64,
        request_digest="c" * 64,
        retry_disposition="forbidden",
        expected_output_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        expected_output_event_class="ModelDelegationResult",
        expected_output_event_index=0,
    )
    raw_payload = {
        "prompt": "Constrained lab task",
        "task_type": "research",
        "source": "codex",
        "correlation_id": correlation_id,
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "dispatch_policy": "backend-pinned-single-attempt.v1",
        "rendered_contract_sha256": "d" * 64,
        "first_effect_authorization_binding": rehydrated.model_dump(mode="json"),
    }

    with pytest.raises(ValidationError, match="first_effect_authorization_binding"):
        ModelDelegateSkillRequest.model_validate(raw_payload)

    port = _RejectedPinnedDispatchPort()
    raw_request = ModelDelegateSkillRequest.model_validate(
        {
            key: value
            for key, value in raw_payload.items()
            if key != "first_effect_authorization_binding"
        }
    )
    terminal = await HandlerDelegateSkill(dispatch_port=port).handle(raw_request)

    assert terminal.status == "failed"
    assert port.dispatch_kwargs is None
