# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cached ladder must follow the bifrost contract that supplies placements."""

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    _DEFAULT_CONFIG_PATH,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)


@pytest.mark.unit
@pytest.mark.parametrize("start_with_placement", [False, True])
@pytest.mark.parametrize("cached_endpoint_resolver", [False, True])
def test_cached_ladder_follows_bifrost_contract_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_with_placement: bool,
    cached_endpoint_resolver: bool,
) -> None:
    if not cached_endpoint_resolver:
        monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: {})
    with_placement = yaml.safe_load(_DEFAULT_CONFIG_PATH.read_text())
    without_placement = yaml.safe_load(_DEFAULT_CONFIG_PATH.read_text())
    for backend in without_placement["backends"]:
        backend.pop("placement", None)
    paths = {}
    for enabled, data in ((True, with_placement), (False, without_placement)):
        path = tmp_path / f"bifrost-{enabled}.yaml"
        path.write_text(yaml.safe_dump(data))
        paths[enabled] = path

    monkeypatch.setattr(routing, "_config", None)
    monkeypatch.setattr(routing, "_config_binding", None)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(paths[start_with_placement]))
    first = routing._get_config()
    assert routing._get_config() is first
    first_frontier = next(t for t in first.tiers if t.name == "cheap_frontier")
    assert bool(first_frontier.models) is start_with_placement

    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(paths[not start_with_placement]))
    second = routing._get_config()
    second_frontier = next(t for t in second.tiers if t.name == "cheap_frontier")
    assert bool(second_frontier.models) is not start_with_placement
    assert routing._get_config() is second
