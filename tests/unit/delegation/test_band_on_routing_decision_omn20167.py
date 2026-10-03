# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20167: the routing decision carries the measured size band.

The band is computed inside ``delta`` from the text the model is given and from
the class contract's thresholds. The request has no band field, so a caller can
neither supply nor override one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.delegation.model_size_band import (
    EnumSizeBand,
    ModelSizeBand,
    ModelSizeBandRequest,
)
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_size_band_compute import (
    HandlerDelegationSizeBand,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.tenant_overlay_resolver import ModelTenantRoutingOverlayBackend

pytestmark = pytest.mark.unit


@pytest.fixture
def routable(monkeypatch: pytest.MonkeyPatch) -> None:
    config = parse_delegation_config_yaml(
        """
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: local-model
                backend_id: local-backend
                max_context_tokens: 65536
                use_for: [summarization]
        """
    )
    task_contract: dict[str, object] = {
        "task_classes": {
            "summarization": {
                "cloud_routing_policy": "allowed",
                "pricing_ceiling_per_1k_tokens": 0.002,
                "escalation_policy": {"max_escalations": 0, "tier_order": ["local"]},
            }
        }
    }
    backends = {
        "local-backend": routing.BifrostBackendRef(
            provider="local",
            endpoint_url="https://local-backend.test/v1/chat/completions",
            model_name="local-model",
            timeout_ms=30_000,
            max_tokens=8192,
        )
    }
    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: task_contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)


def _request(
    *,
    prompt: str = "Summarise the notes.",
    context_pack: str = "",
    acceptance_criteria: tuple[str, ...] = (),
    tenant_id: str | None = None,
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt=prompt,
        task_type="summarization",
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        context_pack=context_pack,
        acceptance_criteria=acceptance_criteria,
        tenant_id=tenant_id,
    )


def test_band_on_routing_decision(routable: None) -> None:
    decision = routing.delta(_request())

    band = decision.size_band
    assert isinstance(band, ModelSizeBand)
    assert band.task_class == "summarization"
    assert band.band is EnumSizeBand.S
    assert band.input_tokens.value == 5
    assert band.units.value == 0
    assert band.steps.value == 0


def test_band_on_routing_decision_is_what_the_node_measures(routable: None) -> None:
    request = _request(
        prompt="Do:\n1. list\n2. count\n",
        context_pack="# One\n# Two\n# Three\n# Four\n",
        acceptance_criteria=("concise", "no_refusal"),
    )

    decision = routing.delta(request)

    measured = HandlerDelegationSizeBand().handle(
        ModelSizeBandRequest(
            task_class=request.task_type,
            prompt=request.prompt,
            context_pack=request.context_pack,
            acceptance_criteria=request.acceptance_criteria,
        )
    )
    assert decision.size_band == measured
    assert decision.size_band is not None
    assert decision.size_band.band is EnumSizeBand.M


def test_band_on_routing_decision_carries_each_feature_and_its_source(
    routable: None,
) -> None:
    band = routing.delta(_request()).size_band

    assert band is not None
    for feature in (band.input_tokens, band.units, band.steps):
        assert feature.source.source == "text_measurement"
        assert feature.threshold.source == "contract"
        assert feature.threshold.reference.startswith(
            "task_class_contracts.v1.yaml#size_band_thresholds."
        )


def test_band_on_routing_decision_follows_the_context_pack(routable: None) -> None:
    small = routing.delta(_request()).size_band
    large = routing.delta(_request(context_pack="x" * 70000)).size_band

    assert small is not None
    assert large is not None
    assert (small.band, large.band) == (EnumSizeBand.S, EnumSizeBand.L)


def test_band_on_routing_decision_survives_escalation_and_the_wire(
    routable: None,
) -> None:
    request = _request(context_pack="x" * 20000)

    first = routing.delta(request, min_tier_name="local")
    decision = ModelRoutingDecision.model_validate_json(first.model_dump_json())

    assert decision.size_band == first.size_band
    assert decision.size_band is not None
    assert decision.size_band.band is EnumSizeBand.M


def test_band_on_routing_decision_of_a_tenant_overlay(routable: None) -> None:
    overlay = ModelTenantRoutingOverlayBackend(
        tenant_id=HOUSE_TENANT_SLUG,
        task_type="summarization",
        backend_id="local-backend",
        endpoint_url="https://local-backend.test/v1/chat/completions",
        model_name="local-model",
        provider="local",
    )

    decision = routing.delta(
        _request(tenant_id=HOUSE_TENANT_SLUG), tenant_overlay=overlay
    )

    assert decision.tier_name == routing.TENANT_OVERLAY_TIER_NAME
    assert decision.size_band is not None
    assert decision.size_band.band is EnumSizeBand.S


def test_band_on_routing_decision_is_none_when_the_contract_is_unreadable(
    routable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routing, "resolve_size_band_thresholds", lambda _name: None)

    decision = routing.delta(_request())

    assert decision.size_band is None
    assert "size_band" not in decision.model_dump(mode="json")


def test_the_request_gains_no_size_field() -> None:
    fields = set(ModelDelegationRequest.model_fields)

    assert not fields & {"size_band", "band", "input_tokens", "units", "steps"}
