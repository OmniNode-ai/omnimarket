# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ``config.lab_fill_orchestrator`` block of the node contract once (OMN-20867)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..models import ModelLabFillOrchestratorConfig

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def _contract() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return data


def orchestrator_config() -> ModelLabFillOrchestratorConfig:
    return ModelLabFillOrchestratorConfig.model_validate(
        _contract()["config"]["lab_fill_orchestrator"]
    )


def fire_plan_topic() -> str:
    """The node's one publish topic: node_lab_fill_plan_compute's fire-plan route."""
    topics = _contract()["event_bus"]["publish_topics"]
    if len(topics) != 1:
        raise ValueError(f"expected one publish topic, the contract declares {topics}")
    return str(topics[0])


__all__: list[str] = ["fire_plan_topic", "orchestrator_config"]
