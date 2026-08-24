# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generation-path model attribution stays bound to the routing authority.

OMN-16492 (follow-up to OMN-16419): the node-generation starting route posts
the contract-declared ``model_routing.served_model_id`` to the wire verbatim —
``resolve_generation_endpoint`` performs no cross-check against the bifrost
backend's ``model_name``, and the call goes through ``HandlerLlmOpenaiCompatible``
(omnibase_infra ``node_llm_inference_effect``), NOT ``HandlerLlmDelegationCall``,
so the OMN-16419 ``MODEL_ATTRIBUTION_MISMATCH`` runtime guard never sees it.
These tests are the guard-equivalent for that path: they bind the generation
contract to the same routing authority the delegation guard reconciles against,
so the two surfaces cannot drift apart silently again (the SGLang endpoint
echoes ANY model string at HTTP 200, so drift is otherwise invisible).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket"

_GENERATION_CONTRACT_PATH = (
    _SRC_ROOT / "nodes" / "node_generation_consumer" / "contract.yaml"
)
_BIFROST_CONTRACT_PATH = _SRC_ROOT / "configs" / "bifrost_delegation.yaml"
_COST_PRICING_PATH = _SRC_ROOT / "cost" / "cost_pricing.yaml"
_ENDPOINT_REGISTRY_PATH = (
    _SRC_ROOT
    / "nodes"
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


def _generation_model_routing() -> dict[str, object]:
    contract = yaml.safe_load(_GENERATION_CONTRACT_PATH.read_text(encoding="utf-8"))
    model_routing = contract["model_routing"]
    assert isinstance(model_routing, dict)
    return model_routing


def _bifrost_backends() -> dict[str, dict[str, object]]:
    contract = yaml.safe_load(_BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    return {backend["backend_id"]: backend for backend in contract["backends"]}


@pytest.mark.unit
def test_generation_served_model_id_matches_routing_authority() -> None:
    """contract.yaml served_model_id == bifrost model_name for its endpoint_ref.

    The delegation path reconciles the backend's ``model_name`` against the
    endpoint's live ``GET /v1/models`` (OMN-16419 fail-closed guard); the
    generation path posts the contract value with no such check. Binding the
    two here means the generation wire model can only be a value the runtime
    guard actively verifies against the live endpoint.
    """
    model_routing = _generation_model_routing()
    endpoint_ref = model_routing["endpoint_ref"]
    assert isinstance(endpoint_ref, str)
    assert endpoint_ref

    backends = _bifrost_backends()
    assert endpoint_ref in backends, (
        f"model_routing.endpoint_ref {endpoint_ref!r} is not a declared bifrost "
        "backend — the generation contract routes through the routing authority"
    )
    assert model_routing["served_model_id"] == backends[endpoint_ref]["model_name"], (
        f"generation contract served_model_id {model_routing['served_model_id']!r} "
        f"!= bifrost backend {endpoint_ref!r} model_name "
        f"{backends[endpoint_ref]['model_name']!r}: the generation path posts the "
        "contract value verbatim and bypasses the delegation-path attribution "
        "guard, so any divergence ships false model attribution (OMN-16419 class)"
    )


@pytest.mark.unit
def test_generation_served_model_id_has_local_cost_pricing_entry() -> None:
    """The generation route's (provider, served_model_id) is priced.

    ``_calculate_cost`` resolves the priced entry with ``allow_unknown=True``,
    so a missing entry silently degrades the cost projection from the correct
    zero-cost local basis to explicit-unknown instead of failing loudly.
    """
    model_routing = _generation_model_routing()
    pricing = yaml.safe_load(_COST_PRICING_PATH.read_text(encoding="utf-8"))
    priced_pairs = {
        (entry["provider"], entry["model_id"]) for entry in pricing["entries"]
    }
    pair = (model_routing["provider"], model_routing["served_model_id"])
    assert pair in priced_pairs, (
        f"cost_pricing.yaml has no entry for {pair!r} — generation cost "
        "attribution degrades to explicit-unknown for every run"
    )


@pytest.mark.unit
def test_swarm_endpoint_registry_carries_no_dead_8001_endpoint() -> None:
    """The swarm registry declares no endpoint on the decommissioned .201:8001.

    GPU1's llama.cpp endpoint is dead (connection refused, OMN-16442); the
    registry schema has no retired flag, so the honest state is absence.
    """
    registry = yaml.safe_load(_ENDPOINT_REGISTRY_PATH.read_text(encoding="utf-8"))
    dead = [ep["id"] for ep in registry["endpoints"] if ":8001" in ep["base_url"]]
    assert not dead, f"endpoints still declared on dead .201:8001: {dead}"


@pytest.mark.unit
def test_swarm_local_primary_model_matches_routing_authority() -> None:
    """The swarm registry's .201:8000 entry names the same served model the
    routing authority declares for local-coder (the live-guarded value)."""
    registry = yaml.safe_load(_ENDPOINT_REGISTRY_PATH.read_text(encoding="utf-8"))
    primary = [
        ep for ep in registry["endpoints"] if ep["base_url"].endswith(":8000/v1")
    ]
    assert primary, "no .201:8000 entry in the swarm endpoint registry"
    local_coder_model = _bifrost_backends()["local-coder"]["model_name"]
    for ep in primary:
        assert ep["model_id"] == local_coder_model, (
            f"swarm registry entry {ep['id']!r} pins model_id {ep['model_id']!r} "
            f"but the routing authority serves {local_coder_model!r} at :8000"
        )
