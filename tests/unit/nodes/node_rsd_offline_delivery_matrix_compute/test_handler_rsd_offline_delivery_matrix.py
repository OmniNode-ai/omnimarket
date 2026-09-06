# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile and golden coverage for the pinned offline C0 validator."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.nodes.node_rsd_offline_delivery_matrix_compute.handlers.handler_rsd_offline_delivery_matrix import (
    RsdOfflineDeliveryMatrixValidationError,
    _derive_bifrost_backend,
    _derive_endpoint,
    _parse_yaml,
    _routing_selection,
    _strict_registry_document,
    validate_rsd_offline_delivery_matrix,
)
from omnimarket.nodes.node_rsd_offline_delivery_matrix_compute.models.model_rsd_offline_delivery_matrix import (
    ModelRsdOfflineDeliveryMatrixInput,
)
from omnimarket.routing.generated_llm_routing_constants import (
    BIFROST_DELEGATION_SHA256,
    ENDPOINT_REGISTRY_SHA256,
    MODEL_REGISTRY_RAW_SHA256,
    MODEL_REGISTRY_SHA256,
    ROUTE_CONTRACT_BUNDLE_SHA256,
    ROUTING_TIERS_SHA256,
)
from omnimarket.rsd.route_contract_bundle import (
    ROUTE_CONTRACT_BUNDLE_DOMAIN,
    route_contract_bundle_bytes,
    route_contract_bundle_sha256,
)
from omnimarket.rsd.route_contract_view import (
    ModelRsdRouteContractView,
    ModelRsdSelectedRoute,
)

_ROOT = Path(__file__).parents[4]
_ROUTING = _ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_BIFROST = _ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_ENDPOINTS = (
    _ROOT
    / "src/omnimarket/nodes/node_swarm_registry_compute/contracts/endpoint_registry.yaml"
)
_REGISTRY = _ROOT / "src/omnimarket/data/model_registry/model_registry_v1.yaml"


def _request(
    *,
    backend_id: str = "local-coder",
    model_registry_key: str = "qwen3-coder-30b",
) -> ModelRsdOfflineDeliveryMatrixInput:
    return ModelRsdOfflineDeliveryMatrixInput(
        route_contract=ModelRsdRouteContractView(
            routing_tiers_yaml=_ROUTING.read_bytes(),
            bifrost_delegation_yaml=_BIFROST.read_bytes(),
            endpoint_registry_yaml=_ENDPOINTS.read_bytes(),
            model_registry_yaml=_REGISTRY.read_bytes(),
            selected_route=ModelRsdSelectedRoute(
                tier_name="local",
                backend_id=backend_id,
                model_registry_key=model_registry_key,
            ),
        )
    )


@pytest.mark.unit
def test_omn_16999_current_pinned_bundle_validates() -> None:
    result = validate_rsd_offline_delivery_matrix(_request())
    assert result.routing_tiers_sha256 == ROUTING_TIERS_SHA256
    assert result.bifrost_delegation_sha256 == BIFROST_DELEGATION_SHA256
    assert result.endpoint_registry_sha256 == ENDPOINT_REGISTRY_SHA256
    assert result.model_registry_raw_sha256 == MODEL_REGISTRY_RAW_SHA256
    assert result.model_registry_hash == MODEL_REGISTRY_SHA256
    assert result.route_contract_bundle_sha256 == ROUTE_CONTRACT_BUNDLE_SHA256
    assert result.non_authorizing is True
    assert result.effects_allowed is False


@pytest.mark.unit
def test_omn_16999_local_ds_v4_flash_uses_backend_served_model() -> None:
    result = validate_rsd_offline_delivery_matrix(
        _request(backend_id="local-ds-v4-flash", model_registry_key="ds-v4-flash")
    )
    assert result.served_model == "deepseek-v4-flash"
    assert result.model_registry_key == "ds-v4-flash"


@pytest.mark.unit
def test_registry_parses_gemini_served_names_and_quoted_decimal_pricing() -> None:
    registry = _strict_registry_document(_parse_yaml(_REGISTRY.read_bytes()))
    profile = registry.get_model("gemini-2.5-flash-lite")
    assert profile.pricing_per_1m_input == Decimal("0.10")
    assert profile.served_model_names == {
        "ai_studio": "gemini-2.5-flash-lite",
        "vertex": "publishers/google/models/gemini-2.5-flash-lite",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    [
        "routing_tiers_yaml",
        "bifrost_delegation_yaml",
        "endpoint_registry_yaml",
        "model_registry_yaml",
    ],
)
def test_fully_self_consistent_forged_bytes_fail_trusted_pins(field: str) -> None:
    request = _request()
    forged = request.route_contract.model_copy(
        update={field: getattr(request.route_contract, field) + b"\n"}
    )
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
        validate_rsd_offline_delivery_matrix(
            request.model_copy(update={"route_contract": forged})
        )


@pytest.mark.unit
@pytest.mark.parametrize("replacement", [b"0x10000", b".nan", b".inf"])
def test_noncanonical_numeric_lexemes_fail_before_semantic_use(
    replacement: bytes,
) -> None:
    request = _request()
    view = request.route_contract.model_copy(
        update={
            "routing_tiers_yaml": request.route_contract.routing_tiers_yaml.replace(
                b"65536", replacement, 1
            )
        }
    )
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
        validate_rsd_offline_delivery_matrix(
            request.model_copy(update={"route_contract": view})
        )


@pytest.mark.unit
def test_unselected_anchor_tag_and_duplicate_fail_closed() -> None:
    request = _request()
    for suffix in (
        b"\nignored: &bad value\n",
        b"\nignored: !!str value\n",
        b"\ntiers: []\n",
    ):
        view = request.route_contract.model_copy(
            update={
                "routing_tiers_yaml": request.route_contract.routing_tiers_yaml + suffix
            }
        )
        with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
            validate_rsd_offline_delivery_matrix(
                request.model_copy(update={"route_contract": view})
            )


@pytest.mark.unit
def test_bifrost_parser_closes_unselected_duplicate_backend() -> None:
    selected = _request().route_contract.selected_route
    malformed = _parse_yaml(_BIFROST.read_bytes())
    malformed["backends"] = [*malformed["backends"], {"backend_id": "local-coder"}]
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
        _derive_bifrost_backend(malformed, selected)


@pytest.mark.unit
def test_endpoint_parser_closes_selected_model_ambiguity() -> None:
    malformed = _parse_yaml(_ENDPOINTS.read_bytes())
    duplicate = dict(malformed["endpoints"][0])
    duplicate["id"] = "different-id"
    malformed["endpoints"] = [*malformed["endpoints"], duplicate]
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
        _derive_endpoint(malformed, duplicate["model_id"])


@pytest.mark.unit
def test_optional_endpoint_and_bifrost_fields_follow_canonical_dtos() -> None:
    endpoint_document = _parse_yaml(_ENDPOINTS.read_bytes())
    endpoint_document["endpoints"][0]["declared_at"] = ""
    assert (
        _derive_endpoint(
            endpoint_document, endpoint_document["endpoints"][0]["model_id"]
        )["declared_at"]
        == ""
    )

    selected = _request().route_contract.selected_route
    bifrost_document = _parse_yaml(_BIFROST.read_bytes())
    selected_backend = next(
        backend
        for backend in bifrost_document["backends"]
        if backend["backend_id"] == selected.backend_id
    )
    selected_backend["api_key_env"] = "CANONICAL_TEST_KEY_NAME"
    selected_backend["api_key_ref"] = "canonical-test-ref"
    selected_backend["secret_ref"] = "canonical-test-ref"
    assert (
        _derive_bifrost_backend(bifrost_document, selected)["backend_id"]
        == selected.backend_id
    )


@pytest.mark.unit
def test_routing_rows_apply_canonical_delegation_cost_semantics() -> None:
    document = _parse_yaml(_ROUTING.read_bytes())
    local_tier = document["tiers"][0]
    local_tier["cost"] = {
        "cost_type": "budgeted",
        "rate_per_1k_usd": 0.01,
        "monthly_cap_usd": 10.0,
        "overage_rate_per_1k_usd": 0.02,
    }
    assert _routing_selection(document, _request().route_contract.selected_route) == (
        "Qwen3.6-35B-A3B",
        65536,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation",
    [
        lambda tier: tier.__setitem__("max_retries", -1),
        lambda tier: tier.__setitem__("eval_before_accept", "true"),
        lambda tier: tier.__setitem__("eval_model", 1),
        lambda tier: tier["models"][0].__setitem__("use_for", ["code_generation", 1]),
        lambda tier: tier["models"][0].__setitem__("fast_path_threshold_tokens", -1),
        lambda tier: tier.__setitem__("cost_per_1k_tokens", float("nan")),
        lambda tier: tier.__setitem__(
            "cost", {"cost_type": "free_local", "rate_per_1k_usd": 0.01}
        ),
        lambda tier: tier.__setitem__("cost", {"cost_type": "metered"}),
        lambda tier: tier.__setitem__(
            "cost",
            {
                "cost_type": "budgeted",
                "rate_per_1k_usd": 0.01,
                "monthly_cap_usd": 1.0,
            },
        ),
        lambda tier: tier.__setitem__("cost", {"cost_type": "unknown"}),
    ],
)
def test_routing_rows_reject_noncanonical_or_invalid_semantics(
    mutation: object,
) -> None:
    document = _parse_yaml(_ROUTING.read_bytes())
    local_tier = document["tiers"][0]
    mutation(local_tier)
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError):
        _routing_selection(document, _request().route_contract.selected_route)


@pytest.mark.unit
def test_routing_use_for_preserves_canonical_scalar_and_repeated_list_semantics() -> (
    None
):
    document = _parse_yaml(_ROUTING.read_bytes())
    local_model = document["tiers"][0]["models"][0]
    local_model["use_for"] = "code_generation"
    assert _routing_selection(document, _request().route_contract.selected_route) == (
        "Qwen3.6-35B-A3B",
        65536,
    )

    local_model["use_for"] = ["code_generation", "code_generation"]
    assert _routing_selection(document, _request().route_contract.selected_route) == (
        "Qwen3.6-35B-A3B",
        65536,
    )


@pytest.mark.unit
@pytest.mark.parametrize("oversized", [10**400, Decimal("1e400"), 1e308])
def test_routing_rejects_oversized_flat_cost_with_stable_failure(
    oversized: object,
) -> None:
    document = _parse_yaml(_ROUTING.read_bytes())
    document["tiers"][0]["cost_per_1k_tokens"] = oversized
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError) as exc_info:
        _routing_selection(document, _request().route_contract.selected_route)
    assert str(exc_info.value) == "offline delivery matrix validation failed"


@pytest.mark.unit
@pytest.mark.parametrize(
    "field", ["rate_per_1k_usd", "monthly_cap_usd", "overage_rate_per_1k_usd"]
)
@pytest.mark.parametrize("oversized", [10**400, Decimal("1e400"), 1e308])
def test_routing_rejects_oversized_typed_cost_fields_with_stable_failure(
    field: str,
    oversized: object,
) -> None:
    document = _parse_yaml(_ROUTING.read_bytes())
    cost = {
        "cost_type": "budgeted",
        "rate_per_1k_usd": 0.01,
        "monthly_cap_usd": 10.0,
        "overage_rate_per_1k_usd": 0.02,
    }
    cost[field] = oversized
    document["tiers"][0]["cost"] = cost
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError) as exc_info:
        _routing_selection(document, _request().route_contract.selected_route)
    assert str(exc_info.value) == "offline delivery matrix validation failed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("container", "field"),
    [
        ("tier", "max_retries"),
        ("model", "max_context_tokens"),
        ("model", "fast_path_threshold_tokens"),
    ],
)
def test_routing_rejects_oversized_integer_route_fields(
    container: str,
    field: str,
) -> None:
    document = _parse_yaml(_ROUTING.read_bytes())
    target = (
        document["tiers"][0]
        if container == "tier"
        else document["tiers"][0]["models"][0]
    )
    target[field] = 10**400
    with pytest.raises(RsdOfflineDeliveryMatrixValidationError) as exc_info:
        _routing_selection(document, _request().route_contract.selected_route)
    assert str(exc_info.value) == "offline delivery matrix validation failed"


@pytest.mark.unit
def test_bundle_is_domain_separated_and_framed() -> None:
    pins = {
        "routing_tiers_raw_sha256": ROUTING_TIERS_SHA256,
        "bifrost_delegation_raw_sha256": BIFROST_DELEGATION_SHA256,
        "endpoint_registry_raw_sha256": ENDPOINT_REGISTRY_SHA256,
        "model_registry_raw_sha256": MODEL_REGISTRY_RAW_SHA256,
        "model_registry_canonical_sha256": MODEL_REGISTRY_SHA256,
    }
    payload = route_contract_bundle_bytes(pins)
    assert ROUTE_CONTRACT_BUNDLE_DOMAIN.encode("ascii") in payload
    assert route_contract_bundle_sha256(pins) == ROUTE_CONTRACT_BUNDLE_SHA256
    assert (
        route_contract_bundle_sha256({**pins, "routing_tiers_raw_sha256": "0" * 64})
        != ROUTE_CONTRACT_BUNDLE_SHA256
    )


@pytest.mark.unit
def test_generator_pins_match_build_resources() -> None:
    assert hashlib.sha256(_ROUTING.read_bytes()).hexdigest() == ROUTING_TIERS_SHA256
    assert (
        hashlib.sha256(_BIFROST.read_bytes()).hexdigest() == BIFROST_DELEGATION_SHA256
    )
    assert (
        hashlib.sha256(_ENDPOINTS.read_bytes()).hexdigest() == ENDPOINT_REGISTRY_SHA256
    )
    assert (
        hashlib.sha256(_REGISTRY.read_bytes()).hexdigest() == MODEL_REGISTRY_RAW_SHA256
    )
