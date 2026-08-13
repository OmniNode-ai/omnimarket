# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Registry-backed topic + tier resolution for ``node_event_emit_effect``.

Reads ``node_emit_daemon/registries/topics.yaml`` (OMN-15965 R1) BY PATH
ONLY, as plain YAML text. This module must never ``import`` anything from
``omnimarket.nodes.node_emit_daemon.*`` -- that whole Python surface
(socket_server.py, publisher_loop.py, event_queue.py, durable_outbox.py,
handlers/handler_emit_daemon.py, __main__.py) is deleted under R5
(OMN-15974), and R1 must not create a dependency R5 would then have to
break. ``registries/topics.yaml`` is the one documented exception -- a data
file, not a Python module; the D2 re-home (moving it out of
``node_emit_daemon/``) is separate, unclaimed scope (see OMN-15964).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


class EnumDurabilityTier(StrEnum):
    """Per-topic durability tier, as declared in the event registry.

    Deliberately re-declared here rather than imported from
    ``node_emit_daemon.models.model_durability`` -- see the module docstring
    for why ``node_event_emit_effect`` must not depend on that package.
    """

    DUTY_CRITICAL = "duty_critical"
    TELEMETRY = "telemetry"


class UnknownEventTypeError(ValueError):
    """Raised when ``event_type`` has no registration in the event registry.

    Fails fast on purpose -- there is no silent default topic.
    """


@dataclass(frozen=True)
class ResolvedTopic:
    """One fan-out target for an ``event_type``: a topic and its tier."""

    topic: str
    tier: EnumDurabilityTier


def default_registry_path() -> Path:
    """Resolve ``registries/topics.yaml`` by path, relative to this package.

    ``node_event_emit_effect`` and ``node_emit_daemon`` are sibling packages
    under ``omnimarket.nodes`` in both the source tree and any installed
    wheel, so sibling-relative resolution survives packaging without a
    hardcoded absolute path and without importing daemon Python modules.
    """
    nodes_root = Path(__file__).resolve().parents[2]
    return nodes_root / "node_emit_daemon" / "registries" / "topics.yaml"


def _parse_registry(
    raw: dict[str, Any], *, source: Path
) -> dict[str, tuple[ResolvedTopic, ...]]:
    events_raw = raw.get("events", {})
    if not isinstance(events_raw, dict):
        raise ValueError(f"{source}: 'events' key must be a dict")

    resolved: dict[str, tuple[ResolvedTopic, ...]] = {}
    for event_type, event_def in events_raw.items():
        if not isinstance(event_def, dict):
            continue
        topics: list[ResolvedTopic] = []
        for rule in event_def.get("fan_out", []):
            topic = rule["topic"]
            tier_raw = rule.get("tier")
            if tier_raw is None:
                raise ValueError(
                    f"{source}: fan-out rule for event '{event_type}' -> "
                    f"'{topic}' has no 'tier'. Every fan-out rule must "
                    "declare a durability tier (duty_critical | telemetry)."
                )
            try:
                tier = EnumDurabilityTier(tier_raw)
            except ValueError as exc:
                valid = ", ".join(t.value for t in EnumDurabilityTier)
                raise ValueError(
                    f"{source}: fan-out rule for event '{event_type}' -> "
                    f"'{topic}' declares unknown tier '{tier_raw}'. Valid "
                    f"tiers: {valid}."
                ) from exc
            topics.append(ResolvedTopic(topic=topic, tier=tier))
        resolved[event_type] = tuple(topics)
    return resolved


@lru_cache(maxsize=8)
def _load_registry(path_str: str) -> dict[str, tuple[ResolvedTopic, ...]]:
    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError(
            f"Event registry not found at {path}. node_event_emit_effect "
            "resolves topics.yaml by path only; the D2 re-home (moving this "
            "file out of node_emit_daemon) has not landed (see OMN-15964)."
        )
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: registry YAML must be a dict")
    return _parse_registry(raw, source=path)


def resolve_event_type(
    event_type: str, *, registry_path: Path | None = None
) -> tuple[ResolvedTopic, ...]:
    """Resolve ``event_type`` to its declared fan-out topics + tiers.

    Raises :class:`UnknownEventTypeError` if ``event_type`` is not
    registered, or is registered with an empty ``fan_out`` list -- no silent
    default topic.
    """
    path = registry_path if registry_path is not None else default_registry_path()
    registry = _load_registry(str(path))
    topics = registry.get(event_type)
    if topics is None:
        raise UnknownEventTypeError(
            f"Unknown event_type '{event_type}'; no registration in {path}."
        )
    if not topics:
        raise UnknownEventTypeError(
            f"event_type '{event_type}' has no fan_out topics declared in {path}."
        )
    return topics


def resolve_tier(topics: tuple[ResolvedTopic, ...]) -> EnumDurabilityTier:
    """Roll up a set of resolved topics into one durability tier.

    Duty-critical dominates: if any fan-out target for an event is
    duty-critical, the whole event is spooled/retried with never-drop
    semantics. Today every event_type in the registry has a single uniform
    tier across its fan-out topics (verified at read time, 2026-08-13); this
    still degrades correctly if that ever changes.
    """
    if any(t.tier is EnumDurabilityTier.DUTY_CRITICAL for t in topics):
        return EnumDurabilityTier.DUTY_CRITICAL
    return EnumDurabilityTier.TELEMETRY


__all__: list[str] = [
    "EnumDurabilityTier",
    "ResolvedTopic",
    "UnknownEventTypeError",
    "default_registry_path",
    "resolve_event_type",
    "resolve_tier",
]
