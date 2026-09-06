# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure C0 validation of canonical delegation-route evidence."""

from __future__ import annotations

import hashlib
from typing import Literal, NoReturn, cast

import yaml
from omnibase_core.models.runtime.golden_chain.model_golden_chain_fixture import (
    ModelGoldenChainProvenance,
)
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config_payload,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_model_registry import (
    ModelLlmModelRegistry,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
    ModelRsdOfflineC0Output,
)


class RsdOfflineC0ValidationError(ValueError):
    """Fail-closed error surface for an incoherent offline C0 evidence set."""

    def __init__(self) -> None:
        super().__init__("offline C0 validation failed")


def _fail() -> NoReturn:
    raise RsdOfflineC0ValidationError()


def _provenance_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _decode_yaml_mapping(payload: bytes) -> dict[str, object]:
    try:
        decoded = payload.decode("utf-8")
        parsed = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError):
        _fail()
    if not isinstance(parsed, dict):
        _fail()
    return cast(dict[str, object], parsed)


def _parse_registry(payload: bytes) -> ModelLlmModelRegistry:
    raw = _decode_yaml_mapping(payload)
    models = raw.get("models")
    if not isinstance(models, dict):
        _fail()
    normalized_models: dict[str, object] = {}
    for key, profile in models.items():
        if not isinstance(key, str) or not isinstance(profile, dict):
            _fail()
        normalized = dict(cast(dict[str, object], profile))
        if normalized.get("model_id") != key:
            _fail()
        normalized_models[key] = normalized
    try:
        return ModelLlmModelRegistry.model_validate(
            {
                "schema_version": raw["schema_version"],
                "model_registry_version": raw["model_registry_version"],
                "pricing_manifest_version": raw["pricing_manifest_version"],
                "observed_at": raw["observed_at"],
                "models": normalized_models,
            }
        )
    except (KeyError, TypeError, ValidationError, ValueError):
        _fail()


def _validate_provenance(
    provenance: ModelGoldenChainProvenance,
    *,
    contract_hash: str,
    overlay_hash: str,
    backend_ref: str,
    served_model: str,
    endpoint: str,
    provider: str,
) -> None:
    if (
        provenance.routing_contract_hash != contract_hash
        or provenance.routing_overlay_hash != overlay_hash
        or provenance.endpoint_ref != backend_ref
        or provenance.model_id != served_model
        or provenance.endpoint != endpoint
        or provenance.provider != provider
    ):
        _fail()


def validate_rsd_offline_c0(request: ModelRsdOfflineC0Input) -> ModelRsdOfflineC0Output:
    """Validate a completed routing decision without selecting or dispatching it.

    The routing reducer remains the only route authority. This function merely
    proves that one reducer output agrees with explicit route-contract bytes and
    recorded golden-chain provenance. It reads no paths, environment variables,
    secret stores, external registries, or network surfaces.
    """
    decision = request.routing_decision
    if not decision.tier_name or not decision.selected_backend_ref:
        _fail()
    contract_hash = _provenance_hash(request.bifrost_contract_yaml)
    overlay_hash = (
        "none"
        if request.bifrost_overlay_yaml is None
        else _provenance_hash(request.bifrost_overlay_yaml)
    )
    try:
        routing_config = parse_delegation_config_yaml(
            request.routing_tiers_yaml.decode("utf-8")
        )
        bifrost = load_bifrost_delegation_config_payload(
            request.bifrost_contract_yaml,
            request.bifrost_overlay_yaml,
            contract_source="offline-c0-routing-contract",
            overlay_source=(
                None
                if request.bifrost_overlay_yaml is None
                else "offline-c0-routing-overlay"
            ),
        )
    except (UnicodeDecodeError, ValueError, ValidationError):
        _fail()

    tier_matches = [
        tier for tier in routing_config.tiers if tier.name == decision.tier_name
    ]
    if len(tier_matches) != 1:
        _fail()
    route_model_matches = [
        model
        for model in tier_matches[0].models
        if model.id == decision.selected_model
        and model.backend_ref == decision.selected_backend_ref
        and model.max_context_tokens == decision.max_context_tokens
    ]
    if len(route_model_matches) != 1:
        _fail()

    backend_matches = [
        backend
        for backend in bifrost.backends
        if backend.backend_id == decision.selected_backend_ref
    ]
    if len(backend_matches) != 1:
        _fail()
    backend = backend_matches[0]
    if (
        backend.tier != decision.tier_name
        or backend.model_name is None
        or backend.endpoint_url is None
        or backend.endpoint_url != decision.endpoint_url
    ):
        _fail()

    registry = _parse_registry(request.model_registry_yaml)
    try:
        profile = registry.get_model(request.model_registry_key)
    except KeyError:
        _fail()
    if profile.model_id != request.model_registry_key or profile.model_name is None:
        _fail()
    expected_served_model: str | None
    if profile.served_model_names is None:
        if request.served_model_environment is not None:
            _fail()
        expected_served_model = profile.model_name
    else:
        if request.served_model_environment is None:
            _fail()
        expected_served_model = profile.served_model_names.get(
            request.served_model_environment
        )
        if expected_served_model is None:
            _fail()
    if (
        expected_served_model != decision.selected_model
        or expected_served_model != backend.model_name
        or decision.max_context_tokens > profile.context_window
    ):
        _fail()
    _validate_provenance(
        request.golden_provenance,
        contract_hash=contract_hash,
        overlay_hash=overlay_hash,
        backend_ref=decision.selected_backend_ref,
        served_model=decision.selected_model,
        endpoint=decision.endpoint_url,
        provider=profile.provider,
    )
    return ModelRsdOfflineC0Output(
        routing_contract_hash=contract_hash,
        routing_overlay_hash=overlay_hash,
        model_registry_hash=registry.model_registry_hash,
        tier_name=decision.tier_name,
        backend_ref=decision.selected_backend_ref,
        routing_tier_model_id=route_model_matches[0].id,
        model_registry_key=request.model_registry_key,
        served_model=decision.selected_model,
        served_model_environment=request.served_model_environment,
    )


class HandlerRsdOfflineC0:
    """Pure ONEX COMPUTE handler for offline C0 validation."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(self, request: ModelRsdOfflineC0Input) -> ModelRsdOfflineC0Output:
        return validate_rsd_offline_c0(request)


__all__ = [
    "HandlerRsdOfflineC0",
    "RsdOfflineC0ValidationError",
    "validate_rsd_offline_c0",
]
