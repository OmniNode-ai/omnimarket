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

from omnimarket.nodes.node_event_emit_effect.errors import (
    AmbiguousTransformError,
    UnresolvableTransformError,
)


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
    """One fan-out target for an ``event_type``: topic, tier, and transform.

    ``transform_name`` is the registry's symbolic ``fan_out[].transform`` name
    (``None`` when the rule declares none). It is resolved to a callable by
    ``enrichment.apply_transform`` at publish time -- the daemon applies the
    transform PER FAN-OUT RULE, so two topics of the same event can carry
    different payloads (OMN-16048).
    """

    topic: str
    tier: EnumDurabilityTier
    transform_name: str | None = None


@dataclass(frozen=True)
class RegisteredEvent:
    """A registry entry: its fan-out targets and its partition-key field.

    ``partition_key_field`` is the registry's declared
    ``events.<event_type>.partition_key_field``. The daemon reads it via
    ``EventRegistry.get_partition_key`` to compute the Kafka message key
    (OMN-16020); ``None`` means the event declares no key.
    """

    topics: tuple[ResolvedTopic, ...]
    partition_key_field: str | None = None


def default_registry_path() -> Path:
    """Resolve ``registries/topics.yaml`` by path, relative to this package.

    ``node_event_emit_effect`` and ``node_emit_daemon`` are sibling packages
    under ``omnimarket.nodes`` in both the source tree and any installed
    wheel, so sibling-relative resolution survives packaging without a
    hardcoded absolute path and without importing daemon Python modules.
    """
    nodes_root = Path(__file__).resolve().parents[2]
    return nodes_root / "node_emit_daemon" / "registries" / "topics.yaml"


def _parse_registry(raw: dict[str, Any], *, source: Path) -> dict[str, RegisteredEvent]:
    events_raw = raw.get("events", {})
    if not isinstance(events_raw, dict):
        raise ValueError(f"{source}: 'events' key must be a dict")

    resolved: dict[str, RegisteredEvent] = {}
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
            topics.append(
                ResolvedTopic(
                    topic=topic,
                    tier=tier,
                    transform_name=rule.get("transform"),
                )
            )
        partition_key_field = event_def.get("partition_key_field")
        if partition_key_field is not None and not isinstance(partition_key_field, str):
            raise ValueError(
                f"{source}: event '{event_type}' declares a non-string "
                f"partition_key_field {partition_key_field!r}."
            )
        resolved[event_type] = RegisteredEvent(
            topics=tuple(topics),
            partition_key_field=partition_key_field,
        )
    return resolved


@lru_cache(maxsize=8)
def _load_registry(path_str: str) -> dict[str, RegisteredEvent]:
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

    Raises :class:`UnknownEventTypeError` only when ``event_type`` has NO
    registration at all in the registry -- no silent default topic.

    An event_type that IS registered but declares an explicitly empty
    ``fan_out`` list (a documented "side-channel only, no Kafka fan-out"
    registration, e.g. ``skill.friction_recorded``) is a deliberate,
    legitimate registration, not an unknown one (OMN-16021): this returns an
    empty tuple rather than raising, so every caller -- present and future --
    degrades gracefully for a zero-fan-out event instead of having to special
    -case it.
    """
    registration = _registration(event_type, registry_path=registry_path)
    return registration.topics


def _registration(
    event_type: str, *, registry_path: Path | None = None
) -> RegisteredEvent:
    path = registry_path if registry_path is not None else default_registry_path()
    registry = _load_registry(str(path))
    registration = registry.get(event_type)
    if registration is None:
        raise UnknownEventTypeError(
            f"Unknown event_type '{event_type}'; no registration in {path}."
        )
    return registration


def resolve_partition_key_field(
    event_type: str, *, registry_path: Path | None = None
) -> str | None:
    """Return the registry-declared ``partition_key_field`` for ``event_type``.

    Mirrors the field ``EventRegistry.get_partition_key`` reads (OMN-16020).
    ``None`` means the event declares no partition key -- the daemon publishes
    those with a null Kafka key, and so must the node.

    Raises :class:`UnknownEventTypeError` if ``event_type`` is not registered,
    matching ``resolve_event_type``'s fail-fast posture (the daemon's
    ``get_partition_key`` raises ``KeyError`` in the same situation).
    """
    return _registration(event_type, registry_path=registry_path).partition_key_field


@lru_cache(maxsize=8)
def _load_topic_transform_index(path_str: str) -> dict[str, frozenset[str | None]]:
    """Invert the registry: topic -> the set of transforms declared FOR it.

    The registry is authored event-first, but a topic override arrives
    topic-first: the caller names a topic and no fan-out rule. This index is
    what lets that topic still carry the redaction its own rule declares
    (OMN-17237, H2).

    The value is a set because a topic may legitimately appear under more than
    one event. A set of size > 1 means those declarations disagree about what
    may reach the topic -- an ambiguity the caller must resolve in the
    registry, not one this module may silently pick a winner for. ``None`` is
    a member of the set like any other value: "declared, needs no transform".
    """
    index: dict[str, set[str | None]] = {}
    for registration in _load_registry(path_str).values():
        for resolved in registration.topics:
            index.setdefault(resolved.topic, set()).add(resolved.transform_name)
    return {topic: frozenset(names) for topic, names in index.items()}


def resolve_override_transform(
    topic: str, event_type: str, *, registry_path: Path | None = None
) -> str | None:
    """Resolve the redaction posture for a caller-supplied topic override.

    ``ModelEmitRequest.topic`` publishes to exactly one topic instead of the
    event's declared fan-out set. Before OMN-17237 that path hardcoded
    ``transform_name=None``, so ANY override published untransformed --
    including onto ``agent.chat.broadcast``'s own observability topic, which
    declares ``strip_body`` precisely so raw chat bodies never land there.
    The escape hatch was a bypass of the entire registry.

    Resolution order, most specific first:

    1. **The topic's own declaration.** If any fan-out rule in the registry
       targets this topic, that rule's transform applies. The topic is what is
       being written to, so what the registry says may reach it wins -- even
       when the override is issued under a different ``event_type``.
    2. **The event's declaration.** For a topic absent from the registry, the
       payload still came from a registered event, and an override must not be
       a way to shed the redaction that event declares. When the event's own
       fan-out rules agree on one posture, it follows the payload to wherever
       the override points it.
    3. **Refuse.** Anything else has no declared posture: an unregistered topic
       under an unregistered (or zero-fan-out) event, or declarations that
       disagree. ``prompt.submitted`` is the live example of disagreement --
       its cmd topic declares no transform, its evt topic declares
       ``strip_prompt``, so an override has no basis to choose. Guessing here
       means guessing permissively, which is the disclosure.

    Raises:
        AmbiguousTransformError: declarations disagree (rule 3).
        UnresolvableTransformError: nothing declares a posture (rule 3).
    """
    path = registry_path if registry_path is not None else default_registry_path()

    declared_for_topic = _load_topic_transform_index(str(path)).get(topic)
    if declared_for_topic is not None:
        if len(declared_for_topic) > 1:
            raise AmbiguousTransformError(
                topic=topic,
                event_type=event_type,
                candidates=tuple(declared_for_topic),
                source="fan-out rules targeting this topic",
            )
        return next(iter(declared_for_topic))

    try:
        registration = _registration(event_type, registry_path=path)
    except UnknownEventTypeError as exc:
        raise UnresolvableTransformError(
            topic=topic,
            event_type=event_type,
            reason=(
                "the topic is absent from the registry and the event_type is "
                "unregistered, so neither declares what may reach it"
            ),
        ) from exc

    declared_for_event = {t.transform_name for t in registration.topics}
    if not declared_for_event:
        raise UnresolvableTransformError(
            topic=topic,
            event_type=event_type,
            reason=(
                "the topic is absent from the registry and the event_type "
                "declares an empty fan_out, so neither declares a transform"
            ),
        )
    if len(declared_for_event) > 1:
        raise AmbiguousTransformError(
            topic=topic,
            event_type=event_type,
            candidates=tuple(declared_for_event),
            source=f"the fan-out rules of event_type {event_type!r}",
        )
    return next(iter(declared_for_event))


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
    "RegisteredEvent",
    "ResolvedTopic",
    "UnknownEventTypeError",
    "default_registry_path",
    "resolve_event_type",
    "resolve_override_transform",
    "resolve_partition_key_field",
    "resolve_tier",
]
