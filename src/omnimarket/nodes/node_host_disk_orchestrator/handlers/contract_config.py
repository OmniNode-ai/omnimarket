# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic names and config of the host disk orchestrator, read from its contract (OMN-23866)."""

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
    found = [t for t in topics if fragment in t]
    if len(found) != 1:
        raise ValueError(f"expected one publish topic with {fragment!r}, found {found}")
    return str(found[0])


def evict_requested_topic() -> str:
    return _publish_topic(".host-disk-evict-requested.")


def slack_publish_topic() -> str:
    return _publish_topic(".slack-publish.")


def run_completed_topic() -> str:
    return _publish_topic(".host-disk-run-completed.")


def schedule_run_topic() -> str:
    return _publish_topic(".schedule-run.")


def decision_overlay_id() -> str:
    return str(_contract()["config"]["host_disk_orchestrator"]["decision_overlay_id"])


__all__: list[str] = [
    "decision_overlay_id",
    "evict_requested_topic",
    "run_completed_topic",
    "schedule_run_topic",
    "slack_publish_topic",
]
