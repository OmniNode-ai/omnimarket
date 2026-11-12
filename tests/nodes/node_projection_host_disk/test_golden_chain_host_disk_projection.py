# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host disk projection (OMN-23865): the event in, the rows and the applied event out."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.events.host_disk_reading import (
    ALL_CLASSES,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_host_disk_writer import (
    HostDiskProjectionWriter,
)
from omnimarket.nodes.node_projection_host_disk.models import (
    ModelHostDiskProjectionApplied,
)

from .host_disk_events import TOPIC, StandInDatabase, later, reading

pytestmark = pytest.mark.unit


def _apply(
    writer: HostDiskProjectionWriter, db: Any, data: dict[str, Any]
) -> dict[str, Any]:
    return asyncio.run(writer._project_event(TOPIC, data, db))


def test_golden_chain_reading_in_rows_and_applied_event_out() -> None:
    writer, db = HostDiskProjectionWriter(), StandInDatabase()
    event = reading(correlation_id="chain-1")
    applied = writer.applied(_apply(writer, db, json.loads(event.model_dump_json())))
    assert applied == ModelHostDiskProjectionApplied(
        correlation_id="chain-1",
        host_name="h202",
        rows_upserted=len(ALL_CLASSES) + 1,
        rows_refused=0,
    )
    assert db.classes[("h202", "uv_cache")]["read_at"] == event.read_at


def test_error_chain_an_older_reading_is_applied_as_refused_not_as_an_error() -> None:
    writer, db = HostDiskProjectionWriter(), StandInDatabase()
    _apply(writer, db, json.loads(reading(at=later(5)).model_dump_json()))
    applied = writer.applied(
        _apply(writer, db, json.loads(reading(correlation_id="late").model_dump_json()))
    )
    assert (applied.rows_upserted, applied.rows_refused) == (0, len(ALL_CLASSES) + 1)


def test_error_chain_a_reading_with_an_unknown_field_is_malformed() -> None:
    data = json.loads(reading().model_dump_json())
    data["invented"] = 1
    with pytest.raises(ValidationError):
        HostDiskProjectionWriter().build_request(TOPIC, data)


def test_trap_the_pure_fold_alone_writes_no_rows() -> None:
    from omnimarket.nodes.node_projection_host_disk.handlers.handler_projection_host_disk import (
        HandlerProjectionHostDisk,
    )

    db = StandInDatabase()
    HandlerProjectionHostDisk().handle(reading())
    assert db.classes == {}
