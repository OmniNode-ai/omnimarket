# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused contract tests for the shared chain assertion helper."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from tests.chains.chain_assert import (
    ChainAssertionError,
    ChainEvent,
    assert_chain,
    assert_error_chain,
)

pytestmark = pytest.mark.unit


class _Started(BaseModel):
    event_id: UUID
    status: str


class _Advanced(BaseModel):
    event_id: UUID
    status: str


class _Completed(BaseModel):
    event_id: UUID
    status: str
    result: int


class _TerminalState(StrEnum):
    COMPLETED = "completed"


class _TypedCompleted(BaseModel):
    status: _TerminalState


def _clean_chain(correlation_id: UUID) -> list[ChainEvent]:
    started_id = uuid4()
    advanced_id = uuid4()
    return [
        ChainEvent(
            topic="test.started",
            event_type="_Started",
            correlation_id=correlation_id,
            causation_id=None,
            payload=_Started(event_id=started_id, status="started"),
        ),
        ChainEvent(
            topic="test.advanced",
            event_type="_Advanced",
            correlation_id=correlation_id,
            causation_id=started_id,
            payload=_Advanced(event_id=advanced_id, status="advanced"),
        ),
        ChainEvent(
            topic="test.completed",
            event_type="_Completed",
            correlation_id=correlation_id,
            causation_id=advanced_id,
            payload=_Completed(event_id=uuid4(), status="completed", result=42),
        ),
    ]


def _assert_clean_chain(events: list[ChainEvent], correlation_id: UUID) -> None:
    assert_chain(
        events,
        expected_event_types=["_Started", "_Advanced", "_Completed"],
        terminal_fields={"status": "completed", "result": 42},
        correlation_id=correlation_id,
        projection_rows=[
            {"sequence": 2, "status": "completed", "ignored": "value"},
            {"sequence": 1, "status": "started", "ignored": "value"},
        ],
        expected_projection_rows=[
            {"sequence": 1, "status": "started"},
            {"sequence": 2, "status": "completed"},
        ],
        ordering_key="sequence",
        bus_history_count=3,
    )


def test_clean_three_event_chain_passes() -> None:
    correlation_id = uuid4()
    _assert_clean_chain(_clean_chain(correlation_id), correlation_id)


def test_extra_event_fails() -> None:
    correlation_id = uuid4()
    events = _clean_chain(correlation_id)
    events.append(events[-1])

    with pytest.raises(ChainAssertionError, match="index 3"):
        _assert_clean_chain(events, correlation_id)


def test_reordered_pair_fails() -> None:
    correlation_id = uuid4()
    events = _clean_chain(correlation_id)
    events[0], events[1] = events[1], events[0]

    with pytest.raises(ChainAssertionError, match="index 0"):
        _assert_clean_chain(events, correlation_id)


def test_changed_correlation_id_fails() -> None:
    correlation_id = uuid4()
    events = _clean_chain(correlation_id)
    changed = events[1]
    events[1] = ChainEvent(
        topic=changed.topic,
        event_type=changed.event_type,
        correlation_id=uuid4(),
        causation_id=changed.causation_id,
        payload=changed.payload,
    )

    with pytest.raises(ChainAssertionError, match="correlation_id"):
        _assert_clean_chain(events, correlation_id)


class _Correlated(BaseModel):
    correlation_id: UUID
    status: str


def test_payload_correlation_id_that_disagrees_with_the_recorder_fails() -> None:
    """The recorder's bookkeeping alone cannot satisfy part (iii)."""
    correlation_id = uuid4()
    events = [
        ChainEvent(
            topic="test.correlated",
            event_type="_Correlated",
            correlation_id=correlation_id,
            causation_id=None,
            payload=_Correlated(correlation_id=uuid4(), status="completed"),
        )
    ]

    with pytest.raises(ChainAssertionError, match="payload correlation_id"):
        assert_chain(
            events,
            expected_event_types=["_Correlated"],
            terminal_fields={"status": "completed"},
            correlation_id=correlation_id,
        )


def test_missing_projection_row_fails() -> None:
    correlation_id = uuid4()

    with pytest.raises(ChainAssertionError, match="projection row count"):
        assert_chain(
            _clean_chain(correlation_id),
            expected_event_types=["_Started", "_Advanced", "_Completed"],
            terminal_fields={"status": "completed"},
            correlation_id=correlation_id,
            projection_rows=[{"sequence": 1, "status": "started"}],
            expected_projection_rows=[
                {"sequence": 1, "status": "started"},
                {"sequence": 2, "status": "completed"},
            ],
            ordering_key="sequence",
        )


def test_empty_terminal_fields_are_refused() -> None:
    correlation_id = uuid4()

    with pytest.raises(ChainAssertionError, match="terminal_fields"):
        assert_chain(
            _clean_chain(correlation_id),
            expected_event_types=["_Started", "_Advanced", "_Completed"],
            terminal_fields={},
            correlation_id=correlation_id,
        )


def test_enum_terminal_field_refuses_a_bare_string() -> None:
    correlation_id = uuid4()
    events = [
        ChainEvent(
            topic="test.completed",
            event_type="_TypedCompleted",
            correlation_id=correlation_id,
            causation_id=None,
            payload=_TypedCompleted(status=_TerminalState.COMPLETED),
        )
    ]

    with pytest.raises(ChainAssertionError, match="terminal field"):
        assert_chain(
            events,
            expected_event_types=["_TypedCompleted"],
            terminal_fields={"status": "completed"},
            correlation_id=correlation_id,
        )


def test_error_chain_requires_an_error_field() -> None:
    correlation_id = uuid4()

    with pytest.raises(ChainAssertionError, match="error field"):
        assert_error_chain(
            _clean_chain(correlation_id),
            expected_event_types=["_Started", "_Advanced", "_Completed"],
            terminal_fields={"status": "completed"},
            correlation_id=correlation_id,
        )


def test_bus_history_count_mismatch_fails() -> None:
    correlation_id = uuid4()

    with pytest.raises(ChainAssertionError, match="bus history count"):
        assert_chain(
            _clean_chain(correlation_id),
            expected_event_types=["_Started", "_Advanced", "_Completed"],
            terminal_fields={"status": "completed"},
            correlation_id=correlation_id,
            bus_history_count=4,
        )


def test_a_passing_chain_logs_its_event_list_for_the_parity_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-19711 P4 reads one JSON line per chain case from CHAIN_EVENT_LOG."""
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHAIN_EVENT_LOG", str(log_path))
    correlation_id = uuid4()
    _assert_clean_chain(_clean_chain(correlation_id), correlation_id)

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records == [
        {
            "case_id": (
                "tests/chains/test_chain_assert.py::"
                "test_a_passing_chain_logs_its_event_list_for_the_parity_tool"
            ),
            "event_types": ["_Started", "_Advanced", "_Completed"],
        }
    ]
