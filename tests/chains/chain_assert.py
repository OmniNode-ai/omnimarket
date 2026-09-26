# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared assertions and recording support for event-chain tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypedDict, Unpack, cast
from uuid import UUID

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel


@dataclass(frozen=True)
class ChainEvent:
    """One typed event observed in an asserted chain."""

    topic: str
    event_type: str
    correlation_id: UUID | str
    causation_id: UUID | str | None
    payload: object


class ChainRecorder:
    """Publish typed events on an in-memory bus and retain their chain metadata."""

    def __init__(self, bus: EventBusInmemory) -> None:
        self._bus = bus
        self._events: list[ChainEvent] = []
        self._started = False

    @property
    def events(self) -> tuple[ChainEvent, ...]:
        """Return recorded events in publication order."""
        return tuple(self._events)

    async def publish(
        self,
        topic: str,
        event: BaseModel,
        *,
        correlation_id: UUID | str,
        causation_id: UUID | str | None = None,
    ) -> None:
        """Publish a JSON-serialized model and record its typed payload."""
        if not self._started:
            await self._bus.start()
            self._started = True
        await self._bus.publish(
            topic,
            key=str(correlation_id).encode(),
            value=event.model_dump_json().encode(),
        )
        self._events.append(
            ChainEvent(
                topic=topic,
                event_type=type(event).__name__,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=event,
            )
        )

    async def bus_history_count(self) -> int:
        """Return the number of messages retained across all bus topics."""
        return len(await self._bus.get_event_history(limit=1_000_000))


class ChainAssertionError(AssertionError):
    """Raised when an event-chain contract is violated."""


class _AssertChainOptions(TypedDict, total=False):
    projection_rows: Sequence[Mapping[str, object]] | None
    expected_projection_rows: Sequence[Mapping[str, object]] | None
    ordering_key: str | None
    bus_history_count: int | None


def _first_difference(actual: Sequence[str], expected: Sequence[str]) -> int:
    for index, (actual_type, expected_type) in enumerate(
        zip(actual, expected, strict=False)
    ):
        if actual_type != expected_type:
            return index
    return min(len(actual), len(expected))


def assert_chain(
    events: Sequence[ChainEvent],
    *,
    expected_event_types: Sequence[str],
    terminal_fields: Mapping[str, object],
    correlation_id: UUID | str,
    projection_rows: Sequence[Mapping[str, object]] | None = None,
    expected_projection_rows: Sequence[Mapping[str, object]] | None = None,
    ordering_key: str | None = None,
    bus_history_count: int | None = None,
) -> None:
    """Assert ordering, terminal facts, lineage, projection, and bus exhaustiveness."""
    if not terminal_fields:
        raise ChainAssertionError("terminal_fields must be non-empty")

    actual_event_types = [event.event_type for event in events]
    expected_types = list(expected_event_types)
    if actual_event_types != expected_types:
        index = _first_difference(actual_event_types, expected_types)
        raise ChainAssertionError(
            f"event types first differ at index {index}: "
            f"actual={actual_event_types!r}, expected={expected_types!r}"
        )

    if not events:
        raise ChainAssertionError("events must contain a terminal event")
    terminal = events[-1].payload
    for field_name, expected_value in terminal_fields.items():
        try:
            actual_value = getattr(terminal, field_name)
        except AttributeError as exc:
            raise ChainAssertionError(
                f"terminal payload {type(terminal).__name__} has no field "
                f"{field_name!r}"
            ) from exc
        enum_type_mismatch = isinstance(actual_value, Enum) and not isinstance(
            expected_value, type(actual_value)
        )
        if enum_type_mismatch or actual_value != expected_value:
            raise ChainAssertionError(
                f"terminal field {field_name!r}: actual={actual_value!r}, "
                f"expected={expected_value!r}"
            )

    earlier_event_ids: set[UUID | str] = set()
    for index, event in enumerate(events):
        if event.correlation_id != correlation_id:
            raise ChainAssertionError(
                f"event at index {index} has correlation_id "
                f"{event.correlation_id!r}; expected {correlation_id!r}"
            )
        # The recorder's correlation_id is what the caller passed to publish();
        # the payload's own field is what a consumer reads. Both must agree, or
        # the check above would only prove the test's bookkeeping.
        payload_correlation_id = getattr(event.payload, "correlation_id", None)
        if payload_correlation_id is not None and str(payload_correlation_id) != str(
            correlation_id
        ):
            raise ChainAssertionError(
                f"event at index {index} ({event.event_type}) carries payload "
                f"correlation_id {payload_correlation_id!r}; expected "
                f"{correlation_id!r}"
            )
        if (
            event.causation_id is not None
            and event.causation_id != correlation_id
            and event.causation_id not in earlier_event_ids
        ):
            raise ChainAssertionError(
                f"event at index {index} has causation_id {event.causation_id!r} "
                "that is neither the correlation_id nor an earlier event id"
            )
        event_id = getattr(event.payload, "event_id", None) or getattr(
            event.payload, "message_id", None
        )
        if isinstance(event_id, (UUID, str)):
            earlier_event_ids.add(event_id)

    if expected_projection_rows is not None:
        if ordering_key is None:
            raise ChainAssertionError(
                "ordering_key is required when expected_projection_rows is given"
            )
        if projection_rows is None:
            raise ChainAssertionError(
                "projection_rows is required when expected_projection_rows is given"
            )
        try:
            actual_rows = sorted(
                projection_rows,
                key=lambda row: cast(Any, row[ordering_key]),
            )
        except KeyError as exc:
            raise ChainAssertionError(
                f"projection row is missing ordering key {ordering_key!r}"
            ) from exc
        if len(actual_rows) != len(expected_projection_rows):
            raise ChainAssertionError(
                f"projection row count: actual={len(actual_rows)}, "
                f"expected={len(expected_projection_rows)}"
            )
        for index, (actual_row, expected_row) in enumerate(
            zip(actual_rows, expected_projection_rows, strict=True)
        ):
            mismatches = {
                key: (actual_row.get(key), expected_value)
                for key, expected_value in expected_row.items()
                if key not in actual_row or actual_row[key] != expected_value
            }
            if mismatches:
                raise ChainAssertionError(
                    f"projection row at index {index} does not contain expected "
                    f"subset: mismatches={mismatches!r}, actual={dict(actual_row)!r}"
                )

    if bus_history_count is not None and bus_history_count != len(events):
        raise ChainAssertionError(
            f"bus history count {bus_history_count} does not equal "
            f"recorded event count {len(events)}"
        )


_ERROR_FIELDS = frozenset(
    {
        "error_code",
        "failure_reason",
        "failure_class",
        "terminal_failure_reason",
        "unrouted_reason",
        "failure_code",
        "terminal_outcome",
    }
)


def assert_error_chain(
    events: Sequence[ChainEvent],
    *,
    expected_event_types: Sequence[str],
    terminal_fields: Mapping[str, object],
    correlation_id: UUID | str,
    **kwargs: Unpack[_AssertChainOptions],
) -> None:
    """Assert a chain whose terminal fields explicitly identify an error."""
    if not _ERROR_FIELDS.intersection(terminal_fields):
        raise ChainAssertionError(
            "terminal_fields must contain at least one recognized error field"
        )
    assert_chain(
        events,
        expected_event_types=expected_event_types,
        terminal_fields=terminal_fields,
        correlation_id=correlation_id,
        **kwargs,
    )


def only(events: Sequence[ChainEvent], event_type: str) -> ChainEvent:
    """Return exactly one event type, lifted from OMN-17802's ``_only`` pattern."""
    matches = [event for event in events if event.event_type == event_type]
    if len(matches) != 1:
        raise ChainAssertionError(
            f"expected exactly one {event_type} in the chain, got "
            f"{[event.event_type for event in events]!r}"
        )
    return matches[0]


chain_obligation = pytest.mark.chain_obligation


__all__ = [
    "ChainAssertionError",
    "ChainEvent",
    "ChainRecorder",
    "assert_chain",
    "assert_error_chain",
    "chain_obligation",
    "only",
]
