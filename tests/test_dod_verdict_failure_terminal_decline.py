# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime boundary failure notice is declined, never dead-lettered (OMN-24235).

When ``node_dod_verify`` fails, the runtime posts its own
``ModelBoundaryFailureTerminal`` on ``onex.evt.omnimarket.dod-verify-completed.v1``
-- the topic this projection consumes for verdicts. Validating that notice as
``ModelDodVerdictWire`` raised three errors and routed every one of them to the
malformed DLQ, so each handler failure manufactured a poison record out of a
known runtime shape.

The fixture here is built from the runtime's OWN model, dumped with
``model_dump(mode="json")``, rather than from a hand-spelled dictionary: the
seam under test is the real payload the boundary publishes, and a copied-out
dict would drift from it the first time the runtime adds a field.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    ROWS_REFUSED_KEY,
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.models import ModelDodVerdictWire
from tests.test_omn18900_dod_verdict_projection import (
    _payload,
    _RecordingDb,
)

pytestmark = pytest.mark.unit


def _failure_notice(**overrides: Any) -> dict[str, Any]:
    """The runtime's failure notice for a failed dod_verify, field for field.

    Built from ``ModelBoundaryFailureTerminal`` itself so the payload cannot
    drift from the shape the consume boundary actually publishes.
    """
    terminal = ModelBoundaryFailureTerminal(
        correlation_id=uuid4(),
        failure_class="ValueError",
        failure_code="ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED",
        retryable=False,
        failure_reason="ticket_id: Field required",
        origin_topic="onex.cmd.omnimarket.dod-verify-start.v1",
    )
    return {**terminal.model_dump(mode="json"), **overrides}


def _write(data: dict[str, Any]) -> tuple[dict[str, Any], _RecordingDb]:
    """Drive the real writer over one payload; return its report and the double."""
    writer = DodVerdictProjectionWriter()
    db = _RecordingDb()
    cast("Any", writer)._db = db
    report = writer.handle(dict(data))
    return report, db


# --------------------------------------------------------------------------
# The verdict path is untouched
# --------------------------------------------------------------------------


def test_golden_a_real_verdict_still_writes_one_row() -> None:
    """The positive control: the decline must not have disturbed the verdict.

    Every decline branch added ahead of the wire validation is one more place
    that could swallow a real verdict, so the capture writes by the same path
    it always did.
    """
    report, db = _write(_payload())
    assert report["rows_upserted"] == 1
    assert report[ROWS_REFUSED_KEY] == 0
    assert len(db.calls) == 1


# --------------------------------------------------------------------------
# The failure notice declines without a write
# --------------------------------------------------------------------------


def test_a_failure_notice_declines_and_writes_nothing() -> None:
    """The runtime's notice is a known shape, not a malformed verdict.

    It must report as a deliberate refusal -- zero rows, one refusal, no
    statement reaching the database, no exception -- rather than landing in
    the malformed DLQ a validation error would send it to.
    """
    report, db = _write(_failure_notice())  # no exception
    assert report["rows_upserted"] == 0
    assert report[ROWS_REFUSED_KEY] == 1
    assert report["dod_verdict_rows"] == []
    assert db.calls == []


def test_a_failure_notice_opens_no_database_connection() -> None:
    """A notice is declined before the pool opens, so a burst of them costs no connection."""

    class _CountingDb(_RecordingDb):
        connects = 0

        async def connect(self) -> None:
            type(self).connects += 1

    writer = DodVerdictProjectionWriter()
    cast("Any", writer)._db = _CountingDb()
    writer.handle(_failure_notice())
    assert _CountingDb.connects == 0


# --------------------------------------------------------------------------
# Malformed payloads still raise, and so reach the DLQ
# --------------------------------------------------------------------------


def test_a_payload_that_is_neither_still_raises() -> None:
    """A verdict with its ticket_id removed is still malformed, not a notice.

    It carries no failure fields, so it validates as neither model and the
    fold must raise from ``handle`` -- the raise is what the runtime routes
    to the malformed DLQ.
    """
    stripped = {k: v for k, v in _payload().items() if k != "ticket_id"}
    with pytest.raises(ValidationError):
        _write(stripped)


def test_a_notice_with_one_extra_key_still_raises() -> None:
    """The decline cannot swallow a malformed verdict that mentions failure fields.

    The runtime model is ``extra="forbid"``, so one unknown key breaks the
    failure-notice read; the wire read then fails on the required fields the
    payload does not carry, and the payload raises instead of declining.
    """
    with pytest.raises(ValidationError):
        _write(_failure_notice(unexpected_field=1))


# --------------------------------------------------------------------------
# The two shapes are disjoint
# --------------------------------------------------------------------------


def test_the_notice_fails_the_wire_model_on_the_three_logged_fields() -> None:
    """The three errors the runtime logged are the notice's missing fields.

    A notice carries ``correlation_id`` and ``status`` and nothing else this
    wire requires, so it fails on exactly ticket_id, started_at and
    completed_at -- which is the defect this ticket fixes, named field by
    field.
    """
    with pytest.raises(ValidationError) as exc_info:
        ModelDodVerdictWire.model_validate(_failure_notice())
    missing = {error["loc"][0] for error in exc_info.value.errors()}
    assert missing == {"ticket_id", "started_at", "completed_at"}


def test_a_real_verdict_fails_the_failure_terminal_model() -> None:
    """A verdict cannot validate as a notice, so the decline cannot eat one.

    The terminal model requires ``failure_class``, ``retryable``,
    ``failure_reason`` and ``origin_topic``, none of which a verdict carries,
    and forbids the extras a verdict does carry. The disjointness is the
    property that makes trying the notice model first safe.
    """
    with pytest.raises(ValidationError):
        ModelBoundaryFailureTerminal.model_validate(_payload())
