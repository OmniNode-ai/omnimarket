# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the lab-fill unit's entry (OMN-24419).

1. Pool snapshot to tick: the hosts the tick is given are the snapshot's hosts the pool admits lanes on, with the
   snapshot's free lane slots (``lane_slots``, as SnapshotFloorPool sums them). The writer side is the lane host pool
   snapshot node (the pool snapshot lane); the real writer handler writes the file here, so a rename of the reading's
   slot fields or the record's bound fails this test.
2. Overlay to tick request: every field of the tick request is either set by the overlay's ``tick_request`` or named
   below as an argument of the fire; a field the request gains and the overlay does not carry fails the second test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_tick import (
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_tick_overlay import (
    ModelLabFillTickOverlay,
)
from omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick import (
    _hosts_from_snapshot,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    accounts,
    advertisement,
    handler,
    host_row,
    overlay,
    pool_request,
)

from .test_runtime_lab_fill_tick import RECORD_SUFFIX, clock_at, tick_overlay

pytestmark = pytest.mark.unit

FIRE_ARGUMENTS = {
    "run_key",
    "fire_id",
    "apply",
    "hosts",
    "selection_holds",
    "ledger_path",
    "approved_work_path",
    "brief_dir",
    "state_path",
}
NOT_DECLARED = {"operator_id", "window_end_epoch_s"}


def test_the_ticks_hosts_are_the_snapshots_admitted_hosts_with_their_slots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pool.json"
    result = handler(
        ads=(
            advertisement("h1", cores=4),
            advertisement("h2", cores=8),
            advertisement("h3", cores=4),
        ),
        rows=(host_row("h1"), host_row("h2"), host_row("h3", available=False)),
        accs=(accounts("h1"), accounts("h2")),
        store=FileSnapshotStore(path, RECORD_SUFFIX),
        overlay_=overlay(snapshot_path=str(path)),
    ).handle(pool_request())
    assert result.record is not None
    free = {row.host: row.free_slots for row in result.record.hosts}
    configured = ModelLabFillTickOverlay.model_validate(tick_overlay(tmp_path))
    hosts = _hosts_from_snapshot(configured, clock_at())
    assert hosts == tuple(
        ModelLabFillTickHost(host=name, free=slots)
        for name, slots in sorted(free.items())
        if slots > 0
    )
    assert {host.host for host in hosts} >= {"h1", "h2"}
    assert "h3" not in {host.host for host in hosts}


def test_the_overlay_carries_every_field_the_tick_request_has() -> None:
    request_fields = set(ModelLabFillTickRequest.model_fields)
    carried = request_fields - FIRE_ARGUMENTS - NOT_DECLARED
    assert carried <= set(ModelLabFillTickOverlay.model_fields)
    assert request_fields >= NOT_DECLARED


def test_the_built_request_takes_the_overlays_values(tmp_path: Path) -> None:
    configured = ModelLabFillTickOverlay.model_validate(tick_overlay(tmp_path))
    request = configured.tick_request(
        run_key="2026-11-14T0900Z",
        fire_id="20261114T090000Z-lab-fill",
        hosts=(ModelLabFillTickHost(host="h1", free=2),),
        selection_holds={"OMN-1": "handed-off"},
        ledger_path="/l",
        approved_work_path="/a",
        brief_dir="/b",
        state_path="",
    )
    for name in set(ModelLabFillTickRequest.model_fields) & set(
        ModelLabFillTickOverlay.model_fields
    ):
        if name in FIRE_ARGUMENTS:
            continue
        assert getattr(request, name) == getattr(configured, name), name
    assert request.selection_holds == {"OMN-1": "handed-off"}
