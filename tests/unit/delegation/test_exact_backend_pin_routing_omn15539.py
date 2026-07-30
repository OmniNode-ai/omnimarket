# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exact initial-backend pin routing for the canonical bus reducer (OMN-15539).

The canonical core request is gaining ``backend_id: str | None``.  This test
uses a temporary request subclass so the omnimarket routing slice remains
testable against the pre-release core pin as well: once the core release lands,
``delta`` observes the same field through the base wire model.

A caller pin has precedence over task/tier selection for the initial decision
only.  The immutable request remains on the workflow during escalation, so a
non-None pin must be deliberately ignored whenever ``min_tier_name`` is set;
otherwise every retry deterministically returns to the failed pinned backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ConfigDict

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)


class _PinnedDelegationRequest(ModelDelegationRequest):
    """Compatibility seam until the paired omnibase_core release is pinned."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)
    backend_id: str | None = None


@pytest.fixture
def exact_pin_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    config = parse_delegation_config_yaml(
        """
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: local-primary-model
                backend_id: local-primary
                max_context_tokens: 8192
                use_for: [research]
              - id: caller-pinned-model
                backend_id: caller-pin
                max_context_tokens: 8192
                use_for: [document]
          - name: cheap_cloud
            cost_per_1k_tokens: 0.002
            models:
              - id: cloud-fallback-model
                backend_id: cloud-fallback
                max_context_tokens: 8192
                use_for: [research]
        """
    )
    task_contract: dict[str, object] = {
        "task_classes": {
            "research": {
                "cloud_routing_policy": "allowed",
                "pricing_ceiling_per_1k_tokens": 0.002,
                "escalation_policy": {
                    "max_escalations": 1,
                    "tier_order": ["local", "cheap_cloud"],
                },
            }
        }
    }
    backends = {
        "local-primary": routing.BifrostBackendRef(
            endpoint_url="https://local-primary.test/v1/chat/completions",
            model_name="local-primary-model",
            timeout_ms=30_000,
            max_tokens=8192,
        ),
        "caller-pin": routing.BifrostBackendRef(
            endpoint_url="https://caller-pin.test/v1/chat/completions",
            model_name="caller-pinned-model",
            timeout_ms=30_000,
            max_tokens=8192,
        ),
        "cloud-fallback": routing.BifrostBackendRef(
            endpoint_url="https://cloud-fallback.test/v1/chat/completions",
            model_name="cloud-fallback-model",
            timeout_ms=30_000,
            max_tokens=8192,
        ),
    }

    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: task_contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)


def _request(*, backend_id: str | None) -> _PinnedDelegationRequest:
    return _PinnedDelegationRequest(
        prompt="Compare the two designs.",
        task_type="research",
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        backend_id=backend_id,
    )


@pytest.mark.unit
def test_initial_route_selects_exact_caller_backend_before_task_tier_policy(
    exact_pin_routing: None,
) -> None:
    request = _request(backend_id="caller-pin")

    decision = routing.delta(request)

    assert decision.selected_backend_ref == "caller-pin"
    assert decision.selected_model == "caller-pinned-model"
    assert decision.tier_name == "local"
    assert "Caller-pinned" in decision.rationale


@pytest.mark.unit
def test_escalation_after_pinned_attempt_resumes_contract_tier_routing(
    exact_pin_routing: None,
) -> None:
    request = _request(backend_id="caller-pin")

    decision = routing.delta(request, min_tier_name="cheap_cloud")

    assert decision.selected_backend_ref == "cloud-fallback"
    assert decision.selected_model == "cloud-fallback-model"
    assert decision.tier_name == "cheap_cloud"
    assert "Caller-pinned" not in decision.rationale


@pytest.mark.unit
def test_unknown_initial_backend_pin_fails_loudly_without_fallback(
    exact_pin_routing: None,
) -> None:
    with pytest.raises(ProtocolConfigurationError, match="does-not-exist"):
        routing.delta(_request(backend_id="does-not-exist"))
