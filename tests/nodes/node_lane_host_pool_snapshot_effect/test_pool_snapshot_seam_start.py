# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the lane start node places a lane from the pool snapshot file the writer wrote (OMN-23664).

The other side is the lane start node's orchestrator (the startnode lane's pieces: node_remote_lane_start_orchestrator and its pool
protocol, ProtocolLaneHostPool). The writer's file is read through the stamped pool adapter, the orchestrator places from it, and
the receipt's CLAIM names the host. Either side changing shape fails here: a reading field the writer renames, a placement bar the
writer's refusal text no longer meets, or a pool read that stops refusing a stale record.

Golden: the lane is placed on the one host the snapshot holds free and the CLAIM names it; the away host is never called.
Error: a snapshot holding only an away host ends NO_HOST (exit 75) with nothing claimed; a stale record raises before any CLAIM.
Failing control: the same snapshot with the away mark dropped places the lane on that host, so the away mark alone keeps it off.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
    StampedSnapshotFileLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    RemoteLaneStartOrchestrator,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    T0,
    Rig,
    cells,
    ledger_effect,
    request,
    settings,
    stream_result,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    LocalLedgerFile,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0 as POOL_T0,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    accounts,
    advertisement,
    handler,
    host_row,
    overlay,
    pool_request,
)

pytestmark = pytest.mark.unit

DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)


def _write(tmp_path: Path, *rows, **kwargs) -> Path:
    """Run the snapshot writer over the given host rows (each with a fresh advertisement and an account) and return the file."""
    path = tmp_path / "pool.json"
    result = handler(
        ads=tuple(advertisement(r.name) for r in rows),
        rows=tuple(rows),
        accs=tuple(accounts(r.name) for r in rows),
        store=FileSnapshotStore(path, ".read-at.json"),
        overlay_=overlay(snapshot_path=str(path)),
        **kwargs,
    ).handle(pool_request())
    assert result.status.value == "written"
    return path


def _rig(tmp_path: Path, path: Path, *, age: int = 0) -> Rig:
    """A lane start rig whose pool is the stamped snapshot file adapter, read ``age`` seconds after the snapshot's read time."""
    rig = Rig(tmp_path, DONE)
    pool = StampedSnapshotFileLaneHostPool(
        path, ".read-at.json", 180, lambda: POOL_T0 + timedelta(seconds=age)
    )
    rig.orchestrator = RemoteLaneStartOrchestrator(
        settings=settings(),
        host_pool=pool,
        ledger=ledger_effect(rig.ledger_path, rig.calls, False, rig.executor),
        ledger_reader=LocalLedgerFile(rig.ledger_path),
        host=HandlerRemoteLaneStartEffect(rig.transport),
        login=None,
        placement=None,
        receipts=rig.receipts,
        echo=rig.printed.append,
        clock=lambda: T0,
        stop_requested=lambda: False,
    )
    return rig


def test_the_lane_is_placed_on_the_free_host_the_snapshot_names(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        host_row("h1"),
        host_row("h2", available=False, unavailable_reason="repair"),
    )
    rig = _rig(tmp_path, path)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    [claim] = rig.ledger_rows("CLAIM")
    assert cells(claim)["host"] == "h1"
    assert rig.host_calls()
    assert not any("h2" in call for call in rig.calls if call.startswith("host:"))


def test_error_a_snapshot_holding_only_an_away_host_ends_no_host_and_claims_nothing(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path, host_row("h2", available=False, unavailable_reason="repair")
    )
    rig = _rig(tmp_path, path)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert result.exit_code == 75
    assert rig.ledger_rows("CLAIM") == []
    assert rig.host_calls() == []


def test_failing_control_the_same_host_without_the_away_mark_takes_the_lane(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, host_row("h2"))
    rig = _rig(tmp_path, path)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert cells(rig.ledger_rows("CLAIM")[0])["host"] == "h2"


def test_error_a_stale_record_ends_pool_unreadable_before_any_claim(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, host_row("h1"))
    rig = _rig(tmp_path, path, age=181)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.POOL_UNREADABLE
    assert result.exit_code == 75
    assert any("REMOTE-LANE-POOL-UNREADABLE" in line for line in rig.printed)
    assert any("stale" in line for line in rig.printed)
    assert rig.ledger_rows("CLAIM") == []
    assert rig.host_calls() == []


def test_error_a_missing_snapshot_file_ends_pool_unreadable_before_any_claim(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, host_row("h1"))
    rig = _rig(tmp_path, path)
    path.unlink()
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.POOL_UNREADABLE
    assert rig.ledger_rows("CLAIM") == []


def test_failing_control_the_same_fresh_record_places_the_lane(tmp_path: Path) -> None:
    path = _write(tmp_path, host_row("h1"))
    rig = _rig(tmp_path, path, age=180)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
