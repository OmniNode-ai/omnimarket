# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ``config.host_hygiene_orchestrator`` block of the node contract (OMN-23707)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def _contract() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return data


def max_hosts() -> int:
    """The reading store bound from the contract config."""
    return int(_contract()["config"]["host_hygiene_orchestrator"]["max_hosts"])


def run_completed_topic() -> str:
    """The run-completed publish topic by name from the contract."""
    topics = _contract()["event_bus"]["publish_topics"]
    for topic in topics:
        if "run-completed" in topic:
            return str(topic)
    raise ValueError(f"run-completed topic not found in publish topics: {topics}")


def schedule_run_topic() -> str:
    """The schedule-run publish topic by name from the contract."""
    topics = _contract()["event_bus"]["publish_topics"]
    for topic in topics:
        if "schedule-run" in topic:
            return str(topic)
    raise ValueError(f"schedule-run topic not found in publish topics: {topics}")


__all__: list[str] = ["max_hosts", "run_completed_topic", "schedule_run_topic"]
