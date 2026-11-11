# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's overlay and the registry units that run it agree (OMN-22921).

A seam test: the overlay declares the run limit its bounds are checked against, and each registry unit that runs the
orchestrator declares the limit it enforces and the host identity input the consumer group is built from. Either side
changing shape turns this red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
EXAMPLE = (
    NODES / "node_pr_landing_tick_orchestrator" / "tick.overlay.redpanda.example.yaml"
)
REGISTRY = NODES / "node_host_installer_registry_units" / "contract.yaml"
UNITS = ("landing-tick-orchestrator", "landing-tick-orchestrator-systemd")


def _units() -> dict[str, dict[str, Any]]:
    contract = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    found = (
        contract["units"]
        if "units" in contract
        else contract["installer_registry"]["units"]
    )
    return {name: found[name] for name in UNITS}


def _limit(unit: dict[str, Any]) -> int:
    schedule = unit["schedule"]
    assert isinstance(schedule, dict)
    return int(unit.get("run_timeout_s") or schedule["run_timeout_s"])


def test_the_example_overlay_validates_and_its_bounds_fit_its_run_limit() -> None:
    overlay = ModelTickOrchestratorOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
    assert overlay.host_env == "ONEX_HOST_NAME"
    assert (overlay.join_timeout_s or 0) + (overlay.poll_timeout_s or 0) + (
        overlay.run_margin_s or 0
    ) <= (overlay.run_limit_s or 0)


@pytest.mark.parametrize("name", UNITS)
def test_each_unit_enforces_the_run_limit_the_overlay_declares(name: str) -> None:
    overlay = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert _limit(_units()[name]) == overlay["run_limit_s"]


@pytest.mark.parametrize("name", UNITS)
def test_each_unit_hands_the_host_identity_the_consumer_group_is_built_from(
    name: str,
) -> None:
    unit = _units()[name]
    overlay = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    host_env = overlay["host_env"]
    assert host_env in unit["environment_inputs"]
    assert host_env in unit["required_environment_inputs"]
