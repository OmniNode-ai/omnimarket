# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: glm-4.5-flash on z.ai's general API, reachable only as a tenant's own key.

z.ai serves ``glm-4.5-flash`` free on its general API (``/api/paas/v4``). We
offer no inference to customers (INV-068) and we are our own customer: the lab is
one tenant, and its z.ai key reaches the model through the same BYOK path a
customer's does (the ``glm`` / ``general_api`` row of the BYOK catalogue), never
through a house backend. This module pins, against the real contract, loader,
catalogue, quota policy and effect handler (no network):

* no house backend or routing rung addresses the general surface;
* the loader refuses any paid z.ai model id on the general surface by name;
* the general API is its own quota domain with its own reading of 1113;
* the lab tenant's BYOK credential routes to the model with paid escalation on
  or off, because a BYOK route is not a paid tier;
* a model the catalogue declares zero-price is booked free, never as a paid
  delegation, while a metered tier's own calls are still booked at the tier rate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    ProviderSurfaceMismatchError,
    load_bifrost_delegation_config,
    reject_backends_off_a_declared_provider_surface,
)
from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.inference.provider_quota_policy import (
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.inference.provider_quota_state import quota_domain_for_endpoint
from omnimarket.nodes.node_delegation_orchestrator.models import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
    _compute_cost,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.routing.byok_provider_backends import (
    byok_declared_price_per_1m,
    resolve_byok_provider_backend,
)
from omnimarket.routing.tenant_overlay_resolver import (
    BYOK_ALL_TASK_TYPES,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_BIFROST_PATH = _ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_ROUTING_TIERS_PATH = _ROOT / "src/omnimarket/configs/routing_tiers.yaml"

_FREE_MODEL = "glm-4.5-flash"
_GENERAL_API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_CODING_PLAN_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_PAID_GENERAL_MODELS = ("glm-5.3-flash", "glm-5.3", "glm-5-turbo", "glm-4.6")
_LAB_TENANT = "onex-lab"
_LAB_KEY_REF = f"cred_{_LAB_TENANT}_glm_0123456789abcdef"


def _bifrost() -> dict[str, Any]:
    loaded = yaml.safe_load(_BIFROST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# -- no house backend ---------------------------------------------------------


def test_no_house_backend_addresses_the_general_surface() -> None:
    """The lab reaches the model as a tenant's own key: no house backend carries it."""
    offenders = []
    for backend in _bifrost()["backends"]:
        url = backend.get("endpoint_url")
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.hostname == "api.z.ai" and parsed.path.startswith("/api/paas/v4"):
            offenders.append(backend["backend_id"])
    assert offenders == []


def test_no_routing_tier_lists_the_free_model() -> None:
    config = parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())
    listed = [
        (tier.name, m.id)
        for tier in config.tiers
        for m in tier.models
        if m.id == _FREE_MODEL
    ]
    assert listed == []


def test_the_catalogue_offers_the_free_model_on_the_general_api_by_default() -> None:
    row = resolve_byok_provider_backend("glm")
    assert row is not None
    assert row.plan == "general_api"
    assert row.model_name == _FREE_MODEL
    assert row.endpoint_url == _GENERAL_API_URL
    assert row.mirrors_house_rung is False


# -- nothing paid on the general surface --------------------------------------


@pytest.mark.parametrize("paid_model", _PAID_GENERAL_MODELS)
def test_the_loader_refuses_a_paid_model_on_the_general_surface_by_name(
    paid_model: str, tmp_path: Path
) -> None:
    data = _bifrost()
    data["backends"].append(
        {
            "backend_id": "cloud-glm-paid-probe",
            "provider": "glm",
            "endpoint_url": _GENERAL_API_URL,
            "model_name": paid_model,
            "secret_ref": "llm.glm.api_key",
            "tier": "cheap_cloud",
            "timeout_ms": 300000,
            "max_tokens": 65536,
        }
    )
    path = tmp_path / "bifrost_delegation.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ProviderSurfaceMismatchError) as excinfo:
        load_bifrost_delegation_config(path)

    rendered = str(excinfo.value)
    assert "cloud-glm-paid-probe" in rendered
    assert paid_model in rendered
    assert _FREE_MODEL in rendered
    assert "balance" in rendered.lower()


def test_the_committed_contract_loads() -> None:
    config = load_bifrost_delegation_config(_BIFROST_PATH)
    assert all(b.model_name != _FREE_MODEL for b in config.backends)


def test_the_loader_admits_the_free_model_on_the_general_surface() -> None:
    rules = _bifrost()["provider_quota_policy"]["providers"]
    backends = [
        {
            "backend_id": "probe",
            "endpoint_url": _GENERAL_API_URL,
            "model_name": _FREE_MODEL,
        }
    ]
    reject_backends_off_a_declared_provider_surface(backends, rules, source="test")


def test_an_overlay_cannot_move_the_coding_plan_model_onto_the_general_surface() -> (
    None
):
    rules = _bifrost()["provider_quota_policy"]["providers"]
    backends = [
        {
            "backend_id": "cloud-glm",
            "endpoint_url": _GENERAL_API_URL,
            "model_name": "glm-5.3-flash",
        }
    ]
    with pytest.raises(ProviderSurfaceMismatchError):
        reject_backends_off_a_declared_provider_surface(backends, rules, source="test")


def test_a_path_on_neither_z_ai_surface_is_still_refused() -> None:
    rules = _bifrost()["provider_quota_policy"]["providers"]
    backends = [
        {
            "backend_id": "stray",
            "endpoint_url": "https://api.z.ai/api/other/v9/chat/completions",
            "model_name": _FREE_MODEL,
        }
    ]
    with pytest.raises(ProviderSurfaceMismatchError):
        reject_backends_off_a_declared_provider_surface(backends, rules, source="test")


# -- quota domain and verdicts ------------------------------------------------


def test_the_general_api_is_its_own_quota_domain() -> None:
    assert quota_domain_for_endpoint(_GENERAL_API_URL) == "zai-general"
    assert quota_domain_for_endpoint(_CODING_PLAN_URL) == "zai"


def _verdict(url: str, code: str, message: str) -> Any:
    return classify_quota_response(
        endpoint_url=url,
        status_code=429,
        body={"error": {"code": code, "message": message}},
        policy=load_provider_quota_policy(),
    )


def test_a_general_api_1302_is_a_declared_retryable_capacity_refusal() -> None:
    verdict = _verdict(_GENERAL_API_URL, "1302", "Rate limit reached for requests")
    assert verdict is not None
    assert verdict.provider_id == "zai-general"
    assert verdict.retryable is True


def test_a_general_api_1113_names_the_free_model_premise_not_a_funding_action() -> None:
    verdict = _verdict(
        _GENERAL_API_URL,
        "1113",
        "Insufficient balance or no resource package. Please recharge.",
    )
    assert verdict is not None
    assert verdict.provider_id == "zai-general"
    assert verdict.retryable is False
    reason = verdict.reason.lower()
    assert "free" in reason
    assert "no paid balance is approved" in reason
    assert "coding plan" not in reason


def test_the_coding_plan_1113_hint_is_unchanged() -> None:
    verdict = _verdict(
        _CODING_PLAN_URL,
        "1113",
        "Insufficient balance or no resource package. Please recharge.",
    )
    assert verdict is not None
    assert verdict.provider_id == "zai"
    assert "coding" in verdict.reason.lower()


# -- the lab tenant's own BYOK credential routes to the model -------------------


class _OverlayReader:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        assert table == TENANT_OVERLAY_TABLE
        return [
            r
            for r in self._rows
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]


def _lab_overlay_row() -> dict[str, object]:
    backend = resolve_byok_provider_backend("glm")
    assert backend is not None
    return {
        "tenant_id": _LAB_TENANT,
        "task_type": BYOK_ALL_TASK_TYPES,
        "backend_id": backend.backend_id,
        "provider": backend.provider,
        "endpoint_url": backend.endpoint_url,
        "model_name": backend.model_name,
        "secret_ref": _LAB_KEY_REF,
        "timeout_ms": backend.timeout_ms,
        "max_tokens": backend.max_tokens,
    }


@pytest.mark.parametrize("allow_paid", [None, "0"])
def test_the_lab_tenants_byok_credential_routes_to_the_free_model(
    allow_paid: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same path as a customer (tenant overlay, tenant_byok, the tenant's own ref).

    A BYOK route is not a paid tier, so switching paid escalation off must not
    exclude it.
    """
    if allow_paid is not None:
        monkeypatch.setenv("ONEX_DELEGATION_ALLOW_PAID", allow_paid)
    overlay = resolve_tenant_overlay(
        _OverlayReader([_lab_overlay_row()]),
        tenant_id=_LAB_TENANT,
        task_type="research",
    )
    assert overlay is not None
    request = ModelDelegationRequest(
        correlation_id=uuid4(),
        prompt="summarise the change",
        task_type="research",
        emitted_at=datetime.now(tz=UTC),
        tenant_id=_LAB_TENANT,
    )
    decision = delta(request, tenant_overlay=overlay)
    assert decision.cost_tier == "tenant_byok"
    assert decision.api_key_ref == _LAB_KEY_REF
    assert decision.endpoint_url == _GENERAL_API_URL
    assert decision.selected_model == _FREE_MODEL


# -- booking: a zero-price model is free --------------------------------------


def test_the_catalogue_declares_the_free_model_zero_price() -> None:
    assert byok_declared_price_per_1m(_GENERAL_API_URL, _FREE_MODEL) == (
        Decimal("0"),
        Decimal("0"),
    )


def test_a_model_the_catalogue_does_not_declare_has_no_declared_price() -> None:
    assert byok_declared_price_per_1m(_GENERAL_API_URL, "glm-5.3-flash") is None
    assert byok_declared_price_per_1m(_CODING_PLAN_URL, _FREE_MODEL) is None


@pytest.mark.parametrize("tier", ["cheap_cloud", "tenant_overlay", "tenant_byok"])
def test_a_zero_price_model_is_booked_free_whatever_tier_label_it_arrives_under(
    tier: str,
) -> None:
    actual, _opus, _savings, basis = _compute_cost(
        _FREE_MODEL, 1000, 500, model_tier=tier, endpoint_url=_GENERAL_API_URL
    )
    assert actual == Decimal("0")
    assert basis is EnumCostBasis.ZERO_MARGINAL_API_COST


def test_a_metered_tiers_own_model_is_still_booked_at_the_tier_rate() -> None:
    actual, _opus, _savings, basis = _compute_cost(
        "gemini-2.5-flash",
        1000,
        500,
        model_tier="cheap_cloud",
        endpoint_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    )
    assert actual > Decimal("0")
    assert basis is EnumCostBasis.CLOUD_API_COST


def test_the_effect_books_a_free_model_call_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.inference.secret_store_resolver import (
        clear_secret_store_resolver_cache,
    )

    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    clear_secret_store_resolver_cache()

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 19},
            },
            latency_ms=5,
        )

    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    monkeypatch.setattr(transport, "probe_served_models", lambda *_a, **_k: None)
    request = ModelLlmDelegationCallRequest(
        request_id="req-free",
        correlation_id="corr-free",
        causation_id="caus-free",
        model_id=_FREE_MODEL,
        endpoint_ref=_GENERAL_API_URL,
        prompt="hi",
        prompt_hash="h",
        task_type="research",
        model_tier="cheap_cloud",
        provider="byok-glm-general",
        timeout_seconds=30.0,
    )
    handler_module = (
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
        "handler_llm_delegation_call"
    )
    with patch(f"{handler_module}._is_endpoint_healthy", return_value=True):
        result = HandlerLlmDelegationCall()(request)

    assert result.success is True
    assert result.actual_cost_usd == Decimal("0")
    assert result.cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
