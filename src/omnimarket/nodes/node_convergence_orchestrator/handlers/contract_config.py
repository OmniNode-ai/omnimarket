# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the publish topics of the convergence orchestrator's node contract (OMN-24198)."""

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


def _publish_topic(fragment: str) -> str:
    topics = _contract()["event_bus"]["publish_topics"]
    for topic in topics:
        if fragment in topic:
            return str(topic)
    raise ValueError(f"{fragment} topic not found in publish topics: {topics}")


def run_completed_topic() -> str:
    """The run-completed publish topic by name from the contract."""
    return _publish_topic("convergence-run-completed")


def schedule_run_topic() -> str:
    """The schedule-run publish topic by name from the contract."""
    return _publish_topic("schedule-run")


__all__: list[str] = ["run_completed_topic", "schedule_run_topic"]
