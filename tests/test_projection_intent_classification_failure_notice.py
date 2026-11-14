# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime boundary failure notice is declined, never dead-lettered (OMN-24332).

When a handler fails, the runtime posts its own ``ModelBoundaryFailureTerminal``
on ``onex.evt.omniintelligence.intent-classified.v1`` -- the topic this
projection consumes for intent-classified events. Validating that notice as
``ModelIntentClassifiedEvent`` raised three errors and routed every one of them
to the malformed DLQ, so each handler failure manufactured a poison record out
of a known runtime shape.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_intent_classification.handlers.handler_intent_classification import (
    IntentClassificationProjectionRunner,
)
from omnimarket.nodes.node_projection_intent_classification.handlers.handler_projection_intent_classification import (
    ROWS_REFUSED_KEY,
    HandlerProjectionIntentClassification,
    ModelIntentClassifiedEvent,
)
from omnimarket.nodes.node_prompt_intent_classify_compute.models.model_intent_classified import (
    ModelIntentClassified,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

TABLE = "intent_classification_events"


def _failure_notice(**overrides: Any) -> dict[str, Any]:
    """The runtime's failure notice, built from the runtime's own model."""
    terminal = ModelBoundaryFailureTerminal(
        correlation_id=uuid4(),
        failure_class="HandlerDispatchFailureError",
        failure_code="ONEX_CORE_063_ITEM_NOT_REGISTERED",
        retryable=False,
        failure_reason="no dispatcher",
        origin_topic="onex.cmd.omniintelligence.claude-hook-event.v1",
    )
    return {**terminal.model_dump(mode="json"), **overrides}


def _real_event() -> dict[str, Any]:
    """A real producer event, built from the producer's own model."""
    event = ModelIntentClassified(
        correlation_id=str(uuid4()),
        session_id="sess-001",
        intent_class="feature",
        intent_category="feature",
        confidence=0.92,
        emitted_at="2026-11-13T23:00:00Z",
    )
    return event.model_dump(mode="json")


def _handle(data: dict[str, Any]) -> tuple[dict[str, Any], InmemoryDatabaseAdapter]:
    """Drive the real handler over one payload; return its report and the db."""
    handler = HandlerProjectionIntentClassification()
    db = InmemoryDatabaseAdapter()
    report = handler.handle({**data, "_db": db})
    return report, db


class _RecordingDb:
    """A fake db that records execute calls."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def _runner() -> tuple[IntentClassificationProjectionRunner, _RecordingDb]:
    """Build the runner with a fake db that records execute calls."""
    runner = IntentClassificationProjectionRunner()
    db = _RecordingDb()
    cast("Any", runner)._db = db
    return runner, db


def test_golden_a_real_event_still_writes_one_row() -> None:
    """The positive control: the decline must not have disturbed the event."""
    report, db = _handle(_real_event())
    assert report["rows_upserted"] == 1
    assert ROWS_REFUSED_KEY not in report
    assert len(db.query(TABLE)) == 1


def test_a_failure_notice_declines_and_writes_nothing() -> None:
    """The runtime's notice is a known shape, not a malformed event."""
    report, db = _handle(_failure_notice())  # no exception
    assert report["rows_upserted"] == 0
    assert report[ROWS_REFUSED_KEY] == 1
    assert db.query(TABLE) == []


def test_a_notice_with_one_extra_key_still_raises() -> None:
    """The decline cannot swallow a malformed notice with an unknown key."""
    with pytest.raises(ValidationError):
        _handle(_failure_notice(unexpected_field=1))


def test_a_real_event_with_session_id_removed_still_raises() -> None:
    """A real event missing session_id is still malformed, not a notice."""
    stripped = {k: v for k, v in _real_event().items() if k != "session_id"}
    with pytest.raises(ValidationError):
        _handle(stripped)


def test_the_notice_fails_the_event_model_on_the_three_logged_fields() -> None:
    """The three errors the runtime logged are the notice's missing fields."""
    with pytest.raises(ValidationError) as exc_info:
        ModelIntentClassifiedEvent.model_validate(_failure_notice())
    missing = {error["loc"][0] for error in exc_info.value.errors()}
    assert missing == {"session_id", "intent_class", "confidence"}


def test_a_real_event_fails_the_failure_terminal_model() -> None:
    """A real event cannot validate as a notice, so the decline cannot eat one."""
    with pytest.raises(ValidationError):
        ModelBoundaryFailureTerminal.model_validate(_real_event())


def test_runner_declines_a_failure_notice() -> None:
    """The runner path declines a notice without executing SQL."""
    runner, db = _runner()
    meta = MessageMeta(partition=0, offset=0, fallback_id="")
    ok = asyncio.run(runner.project_event("topic", _failure_notice(), meta))
    assert ok is True
    assert len(db.calls) == 0


def test_runner_projects_a_real_event() -> None:
    """The runner path projects a real event with one execute call."""
    runner, db = _runner()
    meta = MessageMeta(partition=0, offset=0, fallback_id="")
    ok = asyncio.run(runner.project_event("topic", _real_event(), meta))
    assert ok is True
    assert len(db.calls) == 1
