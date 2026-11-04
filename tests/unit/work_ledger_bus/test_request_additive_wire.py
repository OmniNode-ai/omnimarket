# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998 -- a newer writer and an older ledger host do not refuse each other on an additive field.

On 2026-11-04 the writers on the launching Mac moved to the commit that added
``append_if_absent_row_id`` (OMN-22185) while the ledger host (the work-ledger serve on h200) still ran the
commit before it. The request model forbids extra fields, and the writer put the new field on the wire as
``null`` on every plain append, so the host answered every append with::

    ledger host failure terminal: not a ledger append request: 1 validation error for
    ModelWorkLedgerAppendRequest
    append_if_absent_row_id
      Extra inputs are not permitted [type=extra_forbidden, input_value=None, input_type=NoneType]

Sixteen rows (four lane CLAIMs, five TERMINALs and seven STATUS rows) were refused between 04:08Z and 04:21Z.
A plain append now carries no field an older host lacks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus import bus as ledger_bus
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller, WorkLedgerAppendHost
from tests.unit.work_ledger_bus.test_work_ledger_append_bus import _Bus, _Handler

pytestmark = pytest.mark.unit

RECORDED_ERROR = "Extra inputs are not permitted"


class ModelRequestBeforeAppendIfAbsent(BaseModel):
    """The request model as the ledger host at df5f6b54 read it: no append_if_absent_row_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    ledger_id: str = "rolling-work-ledger"
    rows: str
    requested_by_lane: str
    requesting_host: str
    requested_at: Annotated[datetime, AwareDatetime]


def _plain() -> ModelWorkLedgerAppendRequest:
    request_id = uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=(
            "2026-11-04T04:18:27Z | CLAIM | lane=h051-worktree-fix-0420 | ticket=OMN-19998"
            f" | req={request_id}"
        ),
        requested_by_lane="h051-worktree-fix-0420",
        requesting_host="h200",
        requested_at=datetime(2026, 11, 4, 4, 18, 27, tzinfo=UTC),
    )


def test_plain_append_wire_names_no_append_if_absent_field() -> None:
    wire = _plain().model_dump(mode="json")

    assert "append_if_absent_row_id" not in wire
    assert "append_if_absent_row_id" not in json.loads(_plain().model_dump_json())


def test_a_future_optional_field_is_omitted_until_it_carries_a_value() -> None:
    class ModelFutureRequest(ModelWorkLedgerAppendRequest):
        future_hint: str | None = None

    request = ModelFutureRequest.model_validate(_plain().model_dump(mode="json"))

    for wire in (
        request.model_dump(mode="json"),
        json.loads(request.model_dump_json()),
    ):
        assert "future_hint" not in wire
        ModelRequestBeforeAppendIfAbsent.model_validate(wire)

    used = request.model_copy(update={"future_hint": "requested"})
    for wire in (used.model_dump(mode="json"), json.loads(used.model_dump_json())):
        assert wire["future_hint"] == "requested"
        assert ModelFutureRequest.model_validate(wire) == used
        with pytest.raises(ValidationError, match=RECORDED_ERROR):
            ModelRequestBeforeAppendIfAbsent.model_validate(wire)


def test_plain_append_wire_is_read_by_a_host_older_than_the_field() -> None:
    request = _plain()

    read = ModelRequestBeforeAppendIfAbsent.model_validate(
        request.model_dump(mode="json")
    )

    assert read.request_id == request.request_id
    assert read.rows == request.rows


def test_the_recorded_error_is_what_a_null_field_would_cause() -> None:
    wire = _plain().model_dump(mode="json") | {"append_if_absent_row_id": None}

    with pytest.raises(ValidationError, match=RECORDED_ERROR):
        ModelRequestBeforeAppendIfAbsent.model_validate(wire)


def test_a_set_field_still_reaches_a_host_that_knows_it_and_still_stops_an_old_one() -> (
    None
):
    request = _plain().model_copy(update={"append_if_absent_row_id": "row-1"})
    wire = request.model_dump(mode="json")

    assert wire["append_if_absent_row_id"] == "row-1"
    assert ModelWorkLedgerAppendRequest.model_validate(wire) == request
    # An unconditional append must never stand in for a conditional one.
    with pytest.raises(ValidationError, match=RECORDED_ERROR):
        ModelRequestBeforeAppendIfAbsent.model_validate(wire)


def test_a_request_written_by_the_old_shape_is_read_by_the_new_model() -> None:
    old = ModelRequestBeforeAppendIfAbsent(
        request_id=uuid4(),
        rows="2026-11-04T04:00:00Z | STATUS | lane=x",
        requested_by_lane="x",
        requesting_host="h200",
        requested_at=datetime.now(UTC),
    )

    read = ModelWorkLedgerAppendRequest.model_validate(old.model_dump(mode="json"))

    assert read.append_if_absent_row_id is None


def test_an_outbox_entry_replays_to_an_older_host(tmp_path: Path) -> None:
    """A queued request is stored as the writer wrote it, then replayed after the host is reached."""
    request = _plain()

    path = outbox.queue_request(tmp_path, request)

    stored = json.loads(path.read_text("utf-8"))
    assert "append_if_absent_row_id" not in stored
    ModelRequestBeforeAppendIfAbsent.model_validate(stored)
    assert outbox.pending(tmp_path)[0][1] == request


def test_caller_to_an_older_host_is_accepted_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ledger_bus, "ModelWorkLedgerAppendRequest", ModelRequestBeforeAppendIfAbsent
    )
    request = _plain()

    async def scenario() -> EnumWorkLedgerAppendStatus:
        bus = _Bus()
        await bus.start()
        host = WorkLedgerAppendHost(
            bus,
            _Handler(),
            writer_principals=["developer"],
            operator_principal="operator",
        )
        await host.start()
        caller = WorkLedgerAppendCaller(bus, principal="developer")
        receipt = await caller.append(request, timeout_s=5)
        await caller.stop()
        await host.stop()
        await bus.close()
        return receipt.status

    assert asyncio.run(scenario()) is EnumWorkLedgerAppendStatus.ACCEPTED
