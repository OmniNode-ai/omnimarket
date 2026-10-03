# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: north-mini takes first-choice traffic alongside the ultra rung.

RULING 2026-09-30T15:09:11Z: no model is a fallback-only rung. The committed
contracts place north-mini as an equally weighted spread peer after it received
zero attempts in seven days as the last declared cheap_frontier model. Read
those contracts through the production parsers and exercise the reducer's pick;
the same correlation keys without placement must all land on ultra.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import pytest

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.enums.enum_backend_placement_mode import EnumBackendPlacementMode
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelPlacedDelegationBackend,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.backend_placement import (
    apply_backend_placements,
    spread_groups,
    spread_pick,
    spread_weights,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_RUNG = "openrouter-nemotron-ultra"
_SUPER = "openrouter-nemotron-super"
_PEER = "openrouter-north-mini-code"
_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-17427-north-mini-{i}") for i in range(1000))

type _CommittedConfig = tuple[
    ModelDelegationConfig,
    tuple[ModelPlacedDelegationBackend, ...],
    dict[str, routing.BifrostBackendRef],
]


@pytest.fixture
def committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[_CommittedConfig, None, None]:
    config = parse_delegation_config_yaml(
        (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")
    )
    contract = _CONFIGS / "bifrost_delegation.yaml"
    # Use the committed contract only, without a machine-local endpoint overlay.
    placed = load_bifrost_backend_placements(config_path=contract)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield config, placed, routing._load_bifrost_endpoints()
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _free_tier(config: ModelDelegationConfig) -> ModelRoutingTier:
    return next(tier for tier in config.tiers if tier.name == "cheap_frontier")


def _first_choices(
    config: ModelDelegationConfig,
    placed: tuple[ModelPlacedDelegationBackend, ...],
    backends: dict[str, routing.BifrostBackendRef],
) -> list[str]:
    models = _free_tier(config).models
    peers = spread_groups(placed)
    weights = spread_weights(placed)
    refs: list[str] = []
    for key in _KEYS:
        selected = routing._select_model_for_task(
            models,
            "code_generation",
            100,
            backends,
            require_credential=False,
            spread_key=str(key),
            spread_peers=peers,
            spread_member_weights=weights,
        )
        assert selected is not None
        refs.append(selected.backend_ref)
    return refs


def test_the_peer_is_mirrored_after_both_existing_models(
    committed: _CommittedConfig,
) -> None:
    config, placed, _ = committed
    assert [model.backend_ref for model in _free_tier(config).models] == [_RUNG, _SUPER]
    models = _free_tier(apply_backend_placements(config, placed)).models
    assert [model.backend_ref for model in models] == [_RUNG, _SUPER, _PEER]
    assert models[2].use_for == models[0].use_for
    assert models[2].max_context_tokens == 256000


def test_the_placement_declares_an_equally_weighted_spread_peer(
    committed: _CommittedConfig,
) -> None:
    _, placed, _ = committed
    placement = next(
        backend.placement for backend in placed if backend.backend_id == _PEER
    )
    assert placement.tier == "cheap_frontier"
    assert placement.fallback_for == (_RUNG,)
    assert placement.mode is EnumBackendPlacementMode.SPREAD
    assert placement.weight == 1.0
    assert placement.use_for is None
    assert spread_groups(placed)[_RUNG] == (_PEER,)
    assert spread_weights(placed)[_PEER] == 1.0


def test_code_generation_first_choices_split_evenly_with_placement(
    committed: _CommittedConfig,
) -> None:
    config, placed, backends = committed
    refs = _first_choices(apply_backend_placements(config, placed), placed, backends)
    assert refs == [(_RUNG, _PEER)[spread_pick(str(key), [1.0, 1.0])] for key in _KEYS]
    counts = Counter(refs)
    assert set(counts) == {_RUNG, _PEER}
    assert 0.4 <= counts[_PEER] / len(_KEYS) <= 0.6, counts
    # Positive control: the same keys without placement all choose ultra.
    assert _first_choices(apply_backend_placements(config, ()), (), backends) == [
        _RUNG
    ] * len(_KEYS)


def test_the_mirrored_peer_does_not_offer_code_review(
    committed: _CommittedConfig,
) -> None:
    config, placed, _ = committed
    models = _free_tier(apply_backend_placements(config, placed)).models
    peer = next(model for model in models if model.backend_ref == _PEER)
    assert "code_review" not in peer.use_for
    assert "review" in peer.use_for
