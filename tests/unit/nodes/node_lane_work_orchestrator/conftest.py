# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Helpers for the help step tests: the example chain and a reducer driven to a help request."""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_core.models.lane_work import ModelLaneHelpTier

from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_io import (
    ModelLaneWorkHelpBuildRequest,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)
from tests.unit.nodes.node_lane_work_reducer.conftest_lane_work import HEAD, T0, Driver

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lane_work_orchestrator"
)
EXAMPLE = NODE / "overlay.example.yaml"
TIER_NAMES = (
    "local",
    "glm-small",
    "glm-medium",
    "glm-large",
    "codex-cheapest",
    "claude-small",
    "claude-medium",
    "claude-large",
)


def example_chain() -> tuple[ModelLaneHelpTier, ...]:
    loaded = yaml.safe_load(EXAMPLE.read_text())
    return tuple(ModelLaneHelpTier.model_validate(row) for row in loaded["help_tiers"])


def driver_in_help(
    worker_tier: str, chain: tuple[ModelLaneHelpTier, ...] | None = None
) -> Driver:
    """A reducer driven from `start` to `help_requested` by a `cause_unknown` of the worker."""
    driver = Driver(
        config=ModelLaneWorkReducerConfig(help_tiers=chain or example_chain())
    )
    driver.send("start", worker_tier=worker_tier)
    driver.send("worker_started", lane_name="lane-x")
    driver.send("claim_confirmed")
    driver.send("check_red", head_sha=HEAD, observed_at=T0, ci_read_at=T0)
    driver.send("log_read", red_kind="unknown")
    driver.send("cause_unknown")
    assert driver.name == "help_requested"
    return driver


def build_request(driver: Driver) -> ModelLaneWorkHelpBuildRequest:
    intent = next(i for i in driver.results[-1].intents if i.name == "help_request")
    return ModelLaneWorkHelpBuildRequest(
        state=driver.state, intent=intent, question="why does the push guard refuse?"
    )
