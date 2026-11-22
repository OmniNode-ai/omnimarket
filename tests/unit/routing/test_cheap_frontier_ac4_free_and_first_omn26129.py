# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC4, whole: cheap_frontier stays declared free and its rung is priced 0/0, every ladder that names it is served there, and it comes before every paid tier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)
from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    declared_backends,
)
from tests.placed_routing_config import placed_routing_config

pytestmark = pytest.mark.unit

TIER = "cheap_frontier"
CONFIGS = BIFROST_CONTRACT_PATH.parent
ROUTING_TIERS = CONFIGS / "routing_tiers.yaml"
TASK_CONTRACT = CONFIGS / "task_class_contracts.v1.yaml"
CATALOGUE = Path(__file__).parents[2] / "fixtures" / "openrouter_models_2026_11_20.json"


def _yaml(path: Path) -> dict[str, Any]:
    """Load a YAML document as a mapping."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
        assert isinstance(data, dict)
        return data


def _tiers(path: Path = ROUTING_TIERS) -> dict[str, dict[str, Any]]:
    """Return the tier name to tier mapping from a routing tiers document."""
    return {str(t["name"]): t for t in _yaml(path)["tiers"]}


def _not_free(tiers: dict[str, dict[str, Any]]) -> list[str]:
    """Return messages when the tier is missing, priced, or not free_local."""
    if TIER not in tiers:
        return [f"{TIER} missing"]
    tier = tiers[TIER]
    messages: list[str] = []
    if tier.get("cost_per_1k_tokens") != 0.0:
        messages.append(f"{TIER} cost_per_1k_tokens != 0.0")
    if tier.get("cost", {}).get("cost_type") != "free_local":
        messages.append(f"{TIER} cost_type != free_local")
    return messages


def _rung_models() -> list[str]:
    """Return the model names bound to the tier that are usable as a rung."""
    return [
        str(backend["model_name"])
        for backend in declared_backends().values()
        if backend.get("tier") == TIER
        and backend.get("endpoint_url")
        and not backend.get("explicit_pin_only")
    ]


def _priced(models: list[str], payload: dict[str, Any]) -> list[str]:
    """Return messages for rung models that are unlisted or priced."""
    ids = {row["id"] for row in payload["data"]}
    messages: list[str] = []
    for model in models:
        if model not in ids:
            messages.append(f"{model} not listed")
            continue
        row = next(r for r in payload["data"] if r["id"] == model)
        pricing = row.get("pricing", {})
        if pricing.get("prompt") != "0" or pricing.get("completion") != "0":
            messages.append(f"{model} priced")
    return messages


def _ladders(contract: dict[str, Any]) -> dict[str, list[str]]:
    """Return the task class to escalation tier order mapping."""
    ladders: dict[str, list[str]] = {}
    for task_class, spec in contract["task_classes"].items():
        policy = spec.get("escalation_policy")
        if policy and policy.get("tier_order"):
            ladders[task_class] = list(policy["tier_order"])
    return ladders


def _paid_before_tier(
    ladders: dict[str, list[str]], tiers: dict[str, dict[str, Any]]
) -> list[str]:
    """Return messages for paid tiers that precede the tier in a ladder."""
    messages: list[str] = []
    for task_class, order in ladders.items():
        if TIER not in order:
            continue
        index = order.index(TIER)
        for name in order[:index]:
            cost = tiers.get(name, {}).get("cost", {})
            if cost.get("cost_type") != "free_local":
                messages.append(f"{task_class}: {name} before {TIER}")
    return messages


def _candidates(
    task_class: str, tier: ModelRoutingTier, contract: dict[str, Any]
) -> frozenset[str]:
    """Return the backend refs of the tier that serve the task class."""
    model_ref = routing._get_contract_model_ref(task_class, contract=contract)
    explicit = routing._is_explicit_task_model_override(task_class, contract)
    return frozenset(
        m.backend_ref
        for m in tier.models
        if task_class in m.use_for or (explicit and m.id == model_ref)
    )


def _unserved(
    tier: ModelRoutingTier, ladders: dict[str, list[str]], contract: dict[str, Any]
) -> list[str]:
    """Return task classes whose ladder names the tier but it serves none."""
    return [
        task_class
        for task_class, order in ladders.items()
        if TIER in order and not _candidates(task_class, tier, contract)
    ]


def _placed_tier() -> ModelRoutingTier:
    """Return the placed routing tier named TIER."""
    (tier,) = [t for t in placed_routing_config().tiers if t.name == TIER]
    return tier


def test_the_tier_is_declared_free() -> None:
    """The tier must be declared free with zero cost and free_local type."""
    assert not _not_free(_tiers())


def test_the_rung_is_listed_and_priced_zero() -> None:
    """Every rung model must be listed in the catalogue and priced 0/0."""
    models = _rung_models()
    assert models
    assert not _priced(models, json.loads(CATALOGUE.read_text(encoding="utf-8")))


def test_every_ladder_naming_the_tier_is_served_there() -> None:
    """Every ladder that names the tier must be served by at least one model."""
    contract = _yaml(TASK_CONTRACT)
    ladders = _ladders(contract)
    assert any(TIER in o for o in ladders.values())
    assert not _unserved(_placed_tier(), ladders, contract)


def test_the_tier_comes_before_every_paid_tier() -> None:
    """No paid tier may precede the tier in any ladder."""
    assert not _paid_before_tier(_ladders(_yaml(TASK_CONTRACT)), _tiers())


def test_a_metered_tier_is_caught() -> None:
    """A metered or priced tier must be flagged by the free check."""
    tiers = _tiers()
    tiers[TIER] = {
        **tiers[TIER],
        "cost": {"cost_type": "metered"},
        "cost_per_1k_tokens": 0.002,
    }
    assert len(_not_free(tiers)) == 2


def test_a_priced_or_unlisted_rung_is_caught() -> None:
    """A priced or unlisted rung model must be flagged by the pricing check."""
    payload = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    model = _rung_models()[0]
    for row in payload["data"]:
        if row["id"] == model:
            row["pricing"] = {"prompt": "0.000001", "completion": "0"}
    assert _priced([model], payload) == [f"{model} priced"]
    assert _priced(["vendor/absent:free"], payload) == ["vendor/absent:free not listed"]


def test_an_unserved_tier_is_caught() -> None:
    """A tier with no models must be flagged as unserved."""
    contract = _yaml(TASK_CONTRACT)
    empty = _placed_tier().model_copy(update={"models": []})
    assert _unserved(empty, _ladders(contract), contract)


def test_a_paid_tier_ahead_of_the_tier_is_caught() -> None:
    """A paid tier before the tier in a ladder must be flagged."""
    assert _paid_before_tier({"x": ["local", "cheap_cloud", TIER]}, _tiers()) == [
        f"x: cheap_cloud before {TIER}"
    ]
