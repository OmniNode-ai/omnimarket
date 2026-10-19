# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute both operations of the packaged lab disk growth contract."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_lab_disk_growth_effect import snapshot

pytestmark = pytest.mark.unit


def _resolve(entry: dict[str, Any]) -> tuple[type, type, type]:
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    input_module, _, input_name = entry["input_model"].rpartition(".")
    output_module, _, output_name = entry["output_model"].rpartition(".")
    return (
        handler_type,
        getattr(importlib.import_module(input_module), input_name),
        getattr(importlib.import_module(output_module), output_name),
    )


def test_golden_chain_lab_disk_growth_effect(tmp_path: Path) -> None:
    name = "node_lab_disk_growth_effect"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.lab-disk-growth-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lab-disk-growth-completed.v1"
    ]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    routing = {
        entry["operation"]: entry for entry in contract["handler_routing"]["handlers"]
    }
    assert set(routing) == {"evict_plugin_snapshots", "colocate_uv_cache"}

    handler_type, request_type, result_type = _resolve(
        routing["evict_plugin_snapshots"]
    )
    plugins = tmp_path / "plugins"
    trees = [snapshot(plugins, "omniclaude", n, 1_000_000 + n) for n in range(1, 5)]
    evicted = handler_type(process_references=lambda: []).handle(
        request_type(plugins_dir=plugins, keep_newest=3)
    )
    assert isinstance(evicted, result_type)
    assert evicted.status == "ran"
    assert evicted.evicted == (trees[0].name,)
    assert not trees[0].exists()

    handler_type, request_type, result_type = _resolve(routing["colocate_uv_cache"])
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    config = tmp_path / "uv.toml"
    colocated = handler_type(login_env=lambda: {}, device_of=lambda _path: 1).handle(
        request_type(
            worktrees_root=worktrees, cache_dir=tmp_path / "cache", uv_config=config
        )
    )
    assert isinstance(colocated, result_type)
    assert colocated.status == "configured"
    assert 'link-mode = "hardlink"' in config.read_text()
