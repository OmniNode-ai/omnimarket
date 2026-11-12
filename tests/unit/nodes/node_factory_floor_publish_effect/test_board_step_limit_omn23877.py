# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor step limit and the declared longest path (OMN-23877)."""

from __future__ import annotations

from typing import Any

import yaml

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishContract,
)

CONTRACT = NODE_DIR / "contract.yaml"

MEASURED_MAX_SECONDS = 23
LIMIT_MARGIN = 1.5


def _publish() -> Any:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelFactoryFloorPublishContract.model_validate(raw).runtime


def test_the_factory_floor_step_limit_is_at_least_one_and_a_half_times_the_measured_maximum() -> (
    None
):
    rt = _publish()
    step = next(s for s in rt.steps if s.name == "factory_floor")
    assert step.required
    assert step.timeout_seconds >= MEASURED_MAX_SECONDS * LIMIT_MARGIN


def test_the_declared_longest_path_fits_the_cycle_bound() -> None:
    rt = _publish()
    path = rt.declared_longest_path()
    assert sum(path.values()) <= rt.cycle.bound_seconds, path
