# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh serve tick, its overlay and the host unit that runs it agree (OMN-24220).

A seam test: the node's overlay declares the refresh and bus bounds one pass can spend, the registry unit declares the run
limit it enforces and the module it starts, and the registry contract lists the topics the tick reads and publishes and
its broker grants. Either side changing shape turns this red.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE = NODES / "node_stability_lane_refresh_effect"
REGISTRY = NODES / "node_host_installer_registry_units" / "contract.yaml"
UNIT = "stability-refresh-serve-systemd"


def _registry() -> dict[str, Any]:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))


def _unit() -> dict[str, Any]:
    return _registry()["units"][UNIT]


def _example() -> dict[str, Any]:
    return yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))


def test_the_unit_run_limit_covers_a_whole_refresh_plus_the_bus_bounds() -> None:
    overlay = _example()
    serve = overlay["serve"]
    spent = (
        overlay["refresh_timeout_seconds"]
        + serve["join_timeout_s"]
        + serve["read_timeout_s"]
    )
    assert _unit()["schedule"]["run_timeout_s"] >= spent


def test_the_unit_starts_the_module_that_main_lives_in() -> None:
    unit = _unit()
    assert (NODES.parents[2] / unit["entry_file"]).is_file()
    module = importlib.import_module(unit["module"])
    assert callable(module.main)
    assert unit["probe_argv"] == ["--probe"]


def test_the_contract_lists_and_grants_every_topic_the_tick_uses() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    topics = contract["topics"]
    bus = contract["event_bus"]
    # The runtime wires nothing for this node: a non-empty subscribe list makes infra's auto-wiring build the handler, whose
    # dependencies are constructor parameters, and quarantine it (test_every_node_auto_wires_in_process_omn17427).
    assert bus["subscribe_topics"] == []
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == topics["stability_refresh_requested"]
    )
    answers = {topics[role] for role in topics if role != "stability_refresh_requested"}
    assert set(bus["publish_topics"]) == answers
    rows = contract["broker_acl_grants"][0]["grants"]
    operations = {
        (row["name"], row["operation"])
        for row in rows
        if row["resource_type"] == "TOPIC"
    }
    assert (topics["stability_refresh_requested"], "READ") in operations
    for answer in answers:
        assert (answer, "WRITE") in operations


def test_the_overlay_consumer_group_sits_under_the_granted_group_prefix() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    prefixes = {
        row["name"]
        for row in contract["broker_acl_grants"][0]["grants"]
        if row["resource_type"] == "GROUP" and row["pattern"] == "PREFIXED"
    }
    assert any(
        _example()["serve"]["consumer_group"].startswith(prefix) for prefix in prefixes
    )


def test_the_unit_names_the_overlay_roots_and_the_config_root_the_node_reads() -> None:
    environment = _unit()["environment"]
    assert "ONEX_SKILL_OVERLAY_ROOTS" in environment
    assert "ONEX_WORKSPACE_CONFIG_ROOT" in environment
    assert _unit()["environment_refuse_worktree_paths"] == ["ONEX_SKILL_OVERLAY_ROOTS"]
