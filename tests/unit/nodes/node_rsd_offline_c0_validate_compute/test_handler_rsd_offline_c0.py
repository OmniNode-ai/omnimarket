# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused proof for the pure, non-authorizing OMN-17984 C0 validator."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.models.runtime.golden_chain.model_golden_chain_fixture import (
    ModelGoldenChainProvenance,
)

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.handlers.handler_rsd_offline_c0 import (
    RsdOfflineC0ValidationError,
    validate_rsd_offline_c0,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
)

_ROOT = Path(__file__).parents[4]
_ROUTING_TIERS = (_ROOT / "src/omnimarket/configs/routing_tiers.yaml").read_bytes()
_BIFROST = (_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml").read_bytes()
_REGISTRY = (
    _ROOT / "src/omnimarket/data/model_registry/model_registry_v1.yaml"
).read_bytes()
_ENDPOINT = "https://offline-c0.invalid/v1/chat/completions"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _overlay(*backend_refs: str) -> bytes:
    rows = "\n".join(
        f"  - backend_id: {backend_ref}\n    endpoint_url: {_ENDPOINT}"
        for backend_ref in backend_refs
    )
    return f"backends:\n{rows}\n".encode()


def _decision(
    *,
    model: str = "Qwen3.6-35B-A3B",
    backend_ref: str = "local-coder",
    max_context_tokens: int = 65536,
    endpoint: str = _ENDPOINT,
    tier_name: str = "local",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_type="code_generation",
        selected_model=model,
        # The reducer historically derives this from the model id alone, so it
        # intentionally cannot distinguish the two local Qwen backends. C0
        # proves the raw selected_backend_ref instead.
        selected_backend_id=UUID("00000000-0000-0000-0000-000000000002"),
        endpoint_url=endpoint,
        cost_tier="low",
        max_context_tokens=max_context_tokens,
        max_tokens=65536,
        system_prompt="offline test",
        rationale="offline test",
        tier_name=tier_name,
        selected_backend_ref=backend_ref,
    )


def _request(
    *,
    decision: ModelRoutingDecision | None = None,
    overlay: bytes | None = None,
    model_registry_key: str = "qwen3-coder-30b",
    model_registry_yaml: bytes = _REGISTRY,
    served_model_environment: str | None = None,
    provenance: ModelGoldenChainProvenance | None = None,
) -> ModelRsdOfflineC0Input:
    selected = decision or _decision()
    resolved_overlay = overlay or _overlay(selected.selected_backend_ref)
    proof = provenance or ModelGoldenChainProvenance(
        provider="local",
        model_id=selected.selected_model,
        endpoint_ref=selected.selected_backend_ref,
        endpoint=selected.endpoint_url,
        request_hash="request",
        prompt_hash="prompt",
        routing_contract_hash=_sha256(_BIFROST),
        routing_overlay_hash=_sha256(resolved_overlay),
        recorded_at="2026-09-06T00:00:00Z",
        fixture_version="golden_chain_fixture.v1",
    )
    return ModelRsdOfflineC0Input(
        routing_decision=selected,
        golden_provenance=proof,
        routing_tiers_yaml=_ROUTING_TIERS,
        bifrost_contract_yaml=_BIFROST,
        bifrost_overlay_yaml=resolved_overlay,
        model_registry_yaml=model_registry_yaml,
        model_registry_key=model_registry_key,
        served_model_environment=served_model_environment,
    )


@pytest.mark.unit
def test_c0_validates_canonical_qwen_route_without_authorizing_it() -> None:
    result = validate_rsd_offline_c0(_request())

    assert result.backend_ref == "local-coder"
    assert result.routing_tier_model_id == "Qwen3.6-35B-A3B"
    assert result.model_registry_key == "qwen3-coder-30b"
    assert result.non_authorizing is True
    assert result.effects_allowed is False


@pytest.mark.unit
def test_c0_uses_backend_ref_to_disambiguate_duplicate_qwen_served_ids() -> None:
    decision = _decision(
        backend_ref="local-heavy-reasoning",
        max_context_tokens=8192,
    )
    result = validate_rsd_offline_c0(
        _request(
            decision=decision,
            overlay=_overlay("local-coder", "local-heavy-reasoning"),
        )
    )

    assert result.backend_ref == "local-heavy-reasoning"
    assert result.served_model == "Qwen3.6-35B-A3B"


@pytest.mark.unit
def test_c0_rejects_routing_key_when_it_is_not_the_served_model() -> None:
    decision = _decision(
        model="ds-v4-flash",
        backend_ref="local-ds-v4-flash",
    )

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(
            _request(
                decision=decision,
                overlay=_overlay("local-ds-v4-flash"),
                model_registry_key="ds-v4-flash",
            )
        )


@pytest.mark.unit
def test_c0_rejects_decision_endpoint_drift() -> None:
    decision = _decision(endpoint="https://wrong.invalid/v1/chat/completions")

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(_request(decision=decision))


@pytest.mark.unit
def test_c0_rejects_provenance_hash_drift() -> None:
    decision = _decision()
    overlay = _overlay("local-coder")
    provenance = ModelGoldenChainProvenance(
        provider="local",
        model_id=decision.selected_model,
        endpoint_ref=decision.selected_backend_ref,
        endpoint=decision.endpoint_url,
        request_hash="request",
        prompt_hash="prompt",
        routing_contract_hash="sha256:" + "0" * 64,
        routing_overlay_hash=_sha256(overlay),
        recorded_at="2026-09-06T00:00:00Z",
        fixture_version="golden_chain_fixture.v1",
    )

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(
            _request(decision=decision, overlay=overlay, provenance=provenance)
        )


@pytest.mark.unit
def test_c0_rejects_overlay_only_backend_ids_through_canonical_loader() -> None:
    overlay = _overlay("not-a-declared-backend")

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(_request(overlay=overlay))


@pytest.mark.unit
def test_c0_rejects_unknown_tier() -> None:
    decision = _decision(tier_name="not-a-tier")

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(_request(decision=decision))


@pytest.mark.unit
def test_c0_rejects_unbounded_context_claim() -> None:
    decision = _decision(max_context_tokens=2**63)

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(_request(decision=decision))


@pytest.mark.unit
def test_c0_requires_explicit_environment_for_served_model_mapping() -> None:
    registry = yaml.safe_load(_REGISTRY)
    profile = registry["models"]["qwen3-coder-30b"]
    profile["served_model_names"] = {"offline": "Qwen3.6-35B-A3B"}
    registry_payload = yaml.safe_dump(registry, sort_keys=False).encode()

    result = validate_rsd_offline_c0(
        _request(
            model_registry_yaml=registry_payload,
            served_model_environment="offline",
        )
    )

    assert result.served_model_environment == "offline"
    with pytest.raises(RsdOfflineC0ValidationError):
        validate_rsd_offline_c0(_request(model_registry_yaml=registry_payload))


@pytest.mark.unit
def test_c0_has_no_parallel_route_authority_surfaces() -> None:
    node_root = _ROOT / "src/omnimarket/nodes/node_rsd_offline_c0_validate_compute"
    sources = [
        *(path.read_text(encoding="utf-8") for path in node_root.rglob("*.py")),
        *(path.read_text(encoding="utf-8") for path in node_root.rglob("*.yaml")),
        (
            _ROOT
            / "src/omnimarket/adapters/llm/bifrost/config_loader_bifrost_delegation.py"
        ).read_text(encoding="utf-8"),
    ]
    for forbidden in ("route_contract_bundle", "signer", "swarm_registry"):
        assert all(forbidden not in source for source in sources)
