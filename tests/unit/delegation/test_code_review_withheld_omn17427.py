# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: reviews stay with the calling Claude session, by contract."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.task_class_authority import (
    EnumRoutingAvailabilityStatus,
    TaskClassSelectionError,
    load_task_class_authority,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    resolve_delegation_backend as resolve_local_backend,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
from omnimarket.routing.delegation_backend_resolution import resolve_delegation_backend
from omnimarket.routing.tenant_overlay_resolver import ModelTenantRoutingOverlayBackend
from tests.unit.delegation.test_tier_endpoint_completeness_omn16811 import (
    _TASK_CONTRACT_PATH,
    _available_backends,
    _routable_ladder,
    _routing_config,
    _yaml_mapping,
)

pytestmark = pytest.mark.unit


def test_contract_withholds_code_review_with_the_operator_ruling() -> None:
    authority = load_task_class_authority()
    declared = authority.task_classes["code_review"].routing_availability
    assert declared is not None
    assert declared.status is EnumRoutingAvailabilityStatus.WITHHELD
    assert declared.missing_capability == "measured_adequacy"
    assert "OMN-17427" in declared.tracking
    assert "RULING 2026-10-02T19:33:51Z" in declared.tracking
    assert "stays with the calling Claude session" in " ".join(declared.reason.split())


def test_no_explicit_model_override_can_restore_code_review() -> None:
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    assert "code_review" not in contract["task_model_overrides"]


def test_no_tier_model_claims_code_review() -> None:
    assert all(
        "code_review" not in model.use_for
        for tier in _routing_config().tiers
        for model in tier.models
    )


def test_code_review_selects_no_backend_on_its_whole_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_DELEGATION_ALLOW_PAID", raising=False)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _: True)
    assert _routable_ladder("code_review") == ()
    # Check all tiers as well, so an off-ladder entry cannot hide a stale route.
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    for tier in config.tiers:
        assert (
            routing._select_model_for_task(
                tier.models,
                "code_review",
                0,
                _available_backends(config),
                contract_model_ref=routing._get_contract_model_ref(
                    "code_review", contract=contract
                ),
                contract_model_ref_is_explicit_override=routing._is_explicit_task_model_override(
                    "code_review", contract=contract
                ),
            )
            is None
        )


@pytest.mark.parametrize("explicit", ["code_review", None])
def test_explicit_and_auto_selected_reviews_are_refused(explicit: str | None) -> None:
    authority = load_task_class_authority()
    with pytest.raises(TaskClassSelectionError) as refused:
        authority.resolve_task_type(
            "Review this diff for bugs: the patch changes the authentication check.",
            explicit=explicit,
        )
    assert str(refused.value) == authority.unroutable_refusal("code_review")
    assert "withheld" in str(refused.value)
    assert "OMN-17427" in str(refused.value)


def test_other_code_classes_still_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _: True)
    assert _routable_ladder("code_generation")[0] == ("local", "local-coder")


@pytest.mark.parametrize("surface", list(EnumDelegationSurface))
@pytest.mark.parametrize("with_overlay", [False, True])
def test_every_routing_surface_refuses_before_tenant_overrides(
    surface: EnumDelegationSurface, with_overlay: bool
) -> None:
    request = ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type="code_review",
        prompt="Review this diff for bugs.",
        emitted_at=datetime.now(tz=UTC),
        tenant_id="acme-corp",
    )
    overlay = ModelTenantRoutingOverlayBackend(
        tenant_id="acme-corp",
        task_type="code_review",
        backend_id="customer-review-model",
        provider="openrouter",
        endpoint_url="https://review.contract.test/v1/chat/completions",
        model_name="customer-review-model",
        secret_ref="cred_acme-corp_openrouter_" + "0" * 32,
        timeout_ms=None,
        max_tokens=None,
    )
    with pytest.raises(ProtocolConfigurationError, match=r"withheld.*OMN-17427"):
        routing.delta(
            request,
            tenant_overlay=overlay if with_overlay else None,
            surface=surface,
        )


@pytest.mark.parametrize("backend_id", [None, "local-coder", "byok-openrouter"])
@pytest.mark.parametrize(
    "resolver", [resolve_delegation_backend, resolve_local_backend]
)
def test_direct_backend_and_byok_pins_cannot_delegate_a_review(
    backend_id: str | None, resolver: Callable[..., object]
) -> None:
    with pytest.raises(RuntimeError, match=r"withheld.*OMN-17427"):
        resolver("code_review", backend_id=backend_id)
