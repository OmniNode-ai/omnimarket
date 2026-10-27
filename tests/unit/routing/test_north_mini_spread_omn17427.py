# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 / OMN-20376: north-mini-code is the free tier's only ordinary rung.

It began as a spread peer mirrored from the Nemotron Ultra rung (RULING
2026-09-30T15:09:11Z: no model is a fallback-only rung). OMN-20376 withdrew
Ultra from ``routing_tiers.yaml``, so the placement that mirrored it is gone and
north-mini-code is placed as a rung of its own, with its own ``use_for``. Read the
committed contracts through the production parsers and exercise the reducer's
pick. The spread mechanism itself is covered by the same-model and weighted
spread tests, which build their own placements.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import pytest

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from tests.placed_routing_config import placed_routing_config

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_RETIRED = "openrouter-nemotron-ultra"
_RUNG = "openrouter-north-mini-code"
_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-17427-north-mini-{i}") for i in range(1000))

type _CommittedConfig = tuple[
    ModelDelegationConfig, dict[str, routing.BifrostBackendRef]
]


@pytest.fixture
def committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[_CommittedConfig, None, None]:
    config = placed_routing_config()
    contract = _CONFIGS / "bifrost_delegation.yaml"
    # Use the committed contract only, without a machine-local endpoint overlay.
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield config, routing._load_bifrost_endpoints()
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _free_tier(config: ModelDelegationConfig) -> ModelRoutingTier:
    return next(tier for tier in config.tiers if tier.name == "cheap_frontier")


def test_the_contract_places_north_mini_as_a_rung_of_its_own() -> None:
    placed = load_bifrost_backend_placements(
        config_path=_CONFIGS / "bifrost_delegation.yaml"
    )
    assert [b.backend_id for b in placed] == [_RUNG]
    assert placed[0].placement.fallback_for == ()


def test_north_mini_is_the_only_rung_of_the_free_tier(
    committed: _CommittedConfig,
) -> None:
    config, _ = committed
    assert [model.backend_ref for model in _free_tier(config).models] == [_RUNG]


def test_code_generation_first_choices_all_land_on_north_mini(
    committed: _CommittedConfig,
) -> None:
    config, backends = committed
    models = _free_tier(config).models
    assert backends[_RETIRED].explicit_pin_only is True
    for key in _KEYS:
        selected = routing._select_model_for_task(
            models,
            "code_generation",
            100,
            backends,
            require_credential=False,
            spread_key=str(key),
        )
        assert selected is not None
        assert selected.backend_ref == _RUNG


def test_north_mini_does_not_offer_code_review(
    committed: _CommittedConfig,
) -> None:
    config, _ = committed
    (rung,) = _free_tier(config).models
    assert "code_review" not in rung.use_for
    assert "review" in rung.use_for
