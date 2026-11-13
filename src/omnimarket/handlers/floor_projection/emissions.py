# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's chain emissions as a bus projection (OMN-23877): fold the emission topics into one payload, read it back, and merge it
with the journal file the way the readiness rule needs.

The journal file is health only: its events are read with origin ``unknown``, so a chain that only the file holds never counts as real
work. Only a chain the bus projection holds keeps the origin its publisher set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import JsonValue, ValidationError

from omnimarket.models.floor_17a import ModelNodeEmission

EventKey = tuple[int, str, str, str, datetime]


def _key(event: ModelNodeEmission) -> EventKey:
    return (
        event.piece,
        event.node,
        event.correlation_id,
        event.phase,
        event.emitted_at,
    )


def fold_emissions(
    publications: Sequence[bytes], *, since: datetime, max_events: int
) -> tuple[tuple[ModelNodeEmission, ...], int]:
    """The events the publications hold since ``since``, each once, oldest first, newest ``max_events`` kept; and the count skipped.

    A publication that does not parse is counted and skipped, never read as data.
    """
    skipped = 0
    seen: dict[EventKey, ModelNodeEmission] = {}
    for raw in publications:
        try:
            event = ModelNodeEmission.model_validate_json(raw)
        except (ValueError, ValidationError):
            skipped += 1
            continue
        if event.emitted_at >= since:
            seen.setdefault(_key(event), event)
    ordered = sorted(seen.values(), key=lambda e: e.emitted_at)
    return tuple(ordered[-max_events:]), skipped


def payload_from_emissions(
    events: Sequence[ModelNodeEmission], skipped: int, window_hours: int
) -> dict[str, JsonValue]:
    return {
        "emissions": [e.model_dump(mode="json") for e in events],
        "skipped": skipped,
        "window_hours": window_hours,
    }


def emissions_from_payload(
    payload: Mapping[str, JsonValue],
) -> tuple[ModelNodeEmission, ...]:
    """The events a published payload holds; an entry that does not validate is dropped."""
    held = payload.get("emissions")
    if not isinstance(held, list):
        return ()
    events: list[ModelNodeEmission] = []
    for entry in held:
        try:
            events.append(ModelNodeEmission.model_validate(entry))
        except (ValueError, ValidationError):
            continue
    return tuple(events)


def merge_emissions(
    journal: Sequence[ModelNodeEmission], projected: Sequence[ModelNodeEmission]
) -> tuple[ModelNodeEmission, ...]:
    """The journal's events as health only (origin unknown) and the projection's events as published; an event in both appears once."""
    merged: dict[EventKey, ModelNodeEmission] = {
        _key(e): e.model_copy(update={"origin": "unknown"}) for e in journal
    }
    for event in projected:
        merged[_key(event)] = event
    return tuple(sorted(merged.values(), key=lambda e: e.emitted_at))
