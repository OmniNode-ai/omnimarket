"""Contract-derived event-bus topic helpers for node handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def contract_subscribe_topics(contract_path: Path) -> tuple[str, ...]:
    """Return subscribe topics declared by a node contract."""
    return _contract_topics(contract_path, "subscribe_topics")


def contract_publish_topics(contract_path: Path) -> tuple[str, ...]:
    """Return publish topics declared by a node contract."""
    return _contract_topics(contract_path, "publish_topics")


def _contract_topics(contract_path: Path, key: str) -> tuple[str, ...]:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    event_bus = raw.get("event_bus")
    if not isinstance(event_bus, dict):
        raise ValueError(f"{contract_path} missing event_bus mapping")

    topics: Any = event_bus.get(key)
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError(f"{contract_path} event_bus.{key} must be a string list")
    return tuple(topics)


__all__ = ["contract_publish_topics", "contract_subscribe_topics"]
