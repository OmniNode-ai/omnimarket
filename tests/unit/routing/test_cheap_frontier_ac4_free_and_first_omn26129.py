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


def test_the_tier_is_declared_free() -> None:
    """The tier must be declared free with zero cost and free_local type."""
    assert not _not_free(_tiers())


def test_the_rung_is_listed_and_priced_zero() -> None:
    """Every rung model must be listed in the catalogue and priced 0/0."""
    models = _rung_models()
    assert models
    assert not _priced(models, json.loads(CATALOGUE.read_text(encoding="utf-8")))


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


def _paid_before_free(
    ladders: dict[str, list[str]], tiers: dict[str, dict[str, Any]]
) -> list[str]:
    """Return messages naming paid tiers that appear before a free tier on any ladder."""
    errors: list[str] = []
    for task_class, ladder in ladders.items():
        for i, free in enumerate(ladder):
            if tiers.get(free, {}).get("cost", {}).get("cost_type") == "free_local":
                for paid in ladder[:i]:
                    if (
                        tiers.get(paid, {}).get("cost", {}).get("cost_type")
                        != "free_local"
                    ):
                        errors.append(f"{task_class}: {paid} before {free}")
    return errors


def _candidates(
    task_class: str, tier: ModelRoutingTier, contract: dict[str, Any]
) -> frozenset[str]:
    """Return the backend refs usable for a task class on a tier, honoring explicit overrides."""
    model_ref = routing._get_contract_model_ref(task_class, contract=contract)
    explicit = routing._is_explicit_task_model_override(task_class, contract)
    return frozenset(
        m.backend_ref
        for m in tier.models
        if task_class in m.use_for or (explicit and m.id == model_ref)
    )


def _unserved(
    config_tiers: dict[str, ModelRoutingTier],
    ladders: dict[str, list[str]],
    contract: dict[str, Any],
) -> list[str]:
    """Return messages for ladder tiers that are unknown or serve no backend for a class."""
    errors: list[str] = []
    for task_class, ladder in ladders.items():
        for name in ladder:
            tier = config_tiers.get(name)
            if tier is None:
                errors.append(f"{task_class}: {name} unknown")
            elif not _candidates(task_class, tier, contract):
                errors.append(f"{task_class}: {name} serves no backend")
    return errors


def _placed_tiers() -> dict[str, ModelRoutingTier]:
    """Return the placed routing config tiers keyed by tier name."""
    return {t.name: t for t in placed_routing_config().tiers}


def test_every_tier_on_every_ladder_is_served() -> None:
    """Every tier on every ladder is known and serves a backend for its class."""
    contract = _yaml(TASK_CONTRACT)
    ladders = _ladders(contract)
    assert any(TIER in o for o in ladders.values())
    assert not _unserved(_placed_tiers(), ladders, contract)


def test_every_free_tier_comes_before_every_paid_tier() -> None:
    """No paid tier precedes a free tier on any ladder."""
    assert not _paid_before_free(_ladders(_yaml(TASK_CONTRACT)), _tiers())


def test_an_unserved_tier_is_caught() -> None:
    """A tier with no models is reported as unserving any backend."""
    contract = _yaml(TASK_CONTRACT)
    tiers = _placed_tiers()
    tiers[TIER] = tiers[TIER].model_copy(update={"models": []})
    unserved = _unserved(tiers, _ladders(contract), contract)
    assert unserved
    assert all(TIER in u for u in unserved)


def test_a_paid_tier_ahead_of_a_free_tier_is_caught() -> None:
    """A paid tier ahead of a free tier is reported with the expected messages."""
    planted = {"code_generation": [TIER, "cheap_cloud", "claude", "local"]}
    assert _paid_before_free(planted, _tiers()) == [
        "code_generation: cheap_cloud before local",
        "code_generation: claude before local",
    ]
    assert _paid_before_free({"x": ["local", "cheap_cloud", TIER]}, _tiers()) == [
        f"x: cheap_cloud before {TIER}"
    ]
