# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host disk projection (OMN-23865): the event in, the rows and the applied event out."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_disk_reading import (
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


def test_seam_a_reading_whose_volume_carries_inode_counts_applies_like_one_without() -> (
    None
):
    """The watermark node's volume now carries file-slot counts (OMN-24613); the projection folds the same rows from either shape.

    The seam is the reading model shared by the producer (a lab host at its pinned commit) and this consumer (the dev runtime): a
    payload without the inode keys, as an older producer sends it, parses, and one with them applies the same rows. Failing
    control: ``test_error_chain_a_reading_with_an_unknown_field_is_malformed`` shows what a consumer older than the producer does,
    which is why the runtime is rebuilt before a host moves to a commit that sends the counts.
    """
    data = json.loads(reading(correlation_id="inodes-1").model_dump_json())
    old_shape = json.loads(json.dumps(data))
    for volume in old_shape["volumes"]:
        volume.pop("total_inodes")
        volume.pop("used_inodes")
    for volume in data["volumes"]:
        volume["total_inodes"] = 36421632
        volume["used_inodes"] = 32725171
    writer = HostDiskProjectionWriter()
    old_applied = writer.applied(_apply(writer, StandInDatabase(), old_shape))
    new_applied = writer.applied(_apply(writer, StandInDatabase(), data))
    assert (old_applied.rows_upserted, old_applied.rows_refused) == (
        new_applied.rows_upserted,
        new_applied.rows_refused,
    )
    assert new_applied.rows_upserted == len(ALL_CLASSES) + 1
