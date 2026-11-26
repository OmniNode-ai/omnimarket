# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish runs the cost-and-throughput step in the concurrent group, serves cost-throughput.json, and the simple page draws the
panel with every figure's source and UNREADABLE with its reason."""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml


def _contract(package: str) -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{package}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def test_the_publish_runs_the_cost_throughput_step_in_the_concurrent_group() -> None:
    runtime = _contract("node_factory_floor_publish_effect")["runtime"]
    names = [s["name"] for s in runtime["steps"]]
    assert "cost_throughput" in names
    step = next(s for s in runtime["steps"] if s["name"] == "cost_throughput")
    assert step["required"] is False
    assert step["concurrent"] is True
    assert step["module"] == "omnimarket.nodes.node_floor_cost_throughput_effect"


def test_the_publish_and_the_node_agree_on_the_served_path() -> None:
    publish = _contract("node_factory_floor_publish_effect")["runtime"]
    node = _contract("node_floor_cost_throughput_effect")["runtime"]
    assert (
        publish["cost_throughput_data_relpath"]
        == f"{node['data_dir_relpath']}/{node['output_name']}"
    )
    assert publish["names"]["cost_throughput_data"] == node["output_name"]


def test_the_simple_page_draws_the_panel_with_each_figures_source() -> None:
    page = (
        resources.files("omnimarket.nodes.node_factory_floor_publish_effect")
        .joinpath("page/simple.html")
        .read_text(encoding="utf-8")
    )
    assert 'const COST_DATA = "cost-throughput.json"' in page
    assert '<h2 id="cost-throughput">Cost and throughput</h2>' in page
    assert "7-day trend" in page
    # a figure with no value says UNREADABLE with its reason, and every row names its node, its data and its read time
    assert "UNREADABLE" in page
    assert "costSource(f)" in page
    assert "never read" in page
