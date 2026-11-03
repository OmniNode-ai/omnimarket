# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record's data health: one state per source, thresholds from the node contract, unread never drawn as 0."""

from __future__ import annotations

from datetime import timedelta

import yaml

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorMachineDecl,
    ModelFloorPoolReading,
    ModelFloorRunnerFleet,
)
from omnimarket.models.model_floor_data_health_config import ModelFloorSnapshotBasis
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_floor_data_health import (
    data_health,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_floor_data_health import (
    ModelFloorSourceHealth,
)
from tests.nodes.node_factory_floor_compute.support import (
    CONTRACT,
    NOW,
    ago,
    config,
    request,
)


def _row() -> ModelFloorLedgerRow:
    return ModelFloorLedgerRow(ts=ago(5), kind="STATUS", lane="l", tickets=("OMN-1",))


def _by_id(
    health_sources: tuple[ModelFloorSourceHealth, ...],
) -> dict[str, ModelFloorSourceHealth]:
    return {s.source_id: s for s in health_sources}


def test_data_health_states_and_unread() -> None:
    """An unread source is unavailable with age None and is counted in ``unread``, never as an age of 0."""
    health = data_health(request())
    by_id = _by_id(health.sources)
    # The watcher is always read; nothing else was supplied, so every other source is unread.
    assert by_id["watcher"].state == "available"
    assert by_id["watcher"].age_min == 3
    unread = [s for s in health.sources if s.read_at is None]
    assert unread, "a request with no other source has unread sources"
    for source in unread:
        assert source.state == "unavailable"
        assert source.age_min is None
        assert source.observed_at is None
    assert health.summary.unread == len(unread)
    assert health.summary.unavailable == len(unread)
    assert health.summary.available == 1
    states = {s.state for s in health.sources}
    assert states <= {"available", "partial", "overdue", "unavailable", "snapshot"}
    assert (
        health.summary.available
        + health.summary.partial
        + health.summary.overdue
        + (health.summary.unavailable)
        + health.summary.snapshot
        == len(health.sources)
    )


def test_data_health_a_read_source_is_available_and_carries_its_own_observation() -> (
    None
):
    health = data_health(request(ledger=(_row(),)))
    ledger = _by_id(health.sources)["ledger"]
    assert ledger.state == "available"
    assert ledger.read_at == "2026-10-31T12:00:00Z"
    assert ledger.observed_at == "2026-10-31T11:55:00Z"
    assert ledger.age_min == 0


def test_data_health_overdue_by_the_contract_threshold() -> None:
    limit = config().data_health.overdue_after_min
    fresh = ModelFloorRunnerFleet(
        runners=(), observed_at=NOW - timedelta(minutes=limit)
    )
    old = ModelFloorRunnerFleet(
        runners=(), observed_at=NOW - timedelta(minutes=limit + 1)
    )
    on_limit = _by_id(data_health(request(runner_fleet=fresh)).sources)["runner_fleet"]
    over = _by_id(data_health(request(runner_fleet=old)).sources)["runner_fleet"]
    assert on_limit.state == "available"
    assert over.state == "overdue"
    assert over.age_min == limit + 1
    assert over.observed_at == "2026-10-31T10:29:00Z"
    assert data_health(request(runner_fleet=old)).summary.overdue == 1


def test_data_health_thresholds_are_read_from_the_node_contract() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]["data_health"]
    assert raw["overdue_after_min"] == 90
    assert raw["stale_after_min"] == 360
    health = data_health(request())
    assert health.overdue_after_min == raw["overdue_after_min"]
    assert health.stale_after_min == raw["stale_after_min"]
    # Changing the contract's number changes the verdict: the handler holds no number of its own.
    tight = config().model_copy(
        update={
            "data_health": config().data_health.model_copy(
                update={"overdue_after_min": 2}
            )
        }
    )
    tightened = data_health(request(cfg=tight))
    assert _by_id(tightened.sources)["watcher"].state == "overdue"
    assert tightened.overdue_after_min == 2


def test_data_health_partial_when_only_some_machines_were_read() -> None:
    machines = (
        ModelFloorMachineDecl(host="h1", label="One"),
        ModelFloorMachineDecl(host="h2", label="Two"),
    )
    pool = (
        ModelFloorPoolReading(host="h1", read_at=ago(1)),
        ModelFloorPoolReading(host="h2", read_at=ago(1), error="ssh timeout"),
    )
    source = _by_id(data_health(request(machines=machines, pool=pool)).sources)["pool"]
    assert source.state == "partial"
    assert source.note.startswith("1 of 2 read")
    assert source.read_at is not None


def test_data_health_pool_with_no_answering_host_is_unavailable() -> None:
    pool = (ModelFloorPoolReading(host="h1", read_at=ago(1), error="down"),)
    source = _by_id(data_health(request(pool=pool)).sources)["pool"]
    assert source.state == "unavailable"
    assert source.age_min is None
    assert source.read_at is None


def test_data_health_snapshot_keeps_a_date_only_basis_as_a_date() -> None:
    issue = ModelFloorLinearIssue(id="OMN-1", status="Done", title="t")
    health = data_health(
        request(
            linear=(issue,),
            snapshot_bases=(
                ModelFloorSnapshotBasis(source_id="linear", basis="2026-10-30"),
            ),
        )
    )
    linear = _by_id(health.sources)["linear"]
    assert linear.state == "snapshot"
    assert linear.observed_at == "2026-10-30"
    assert linear.age_min is None
    assert health.summary.snapshot == 1


def test_data_health_a_declared_snapshot_never_read_is_unavailable() -> None:
    linear = _by_id(data_health(request()).sources)["linear"]
    assert linear.state == "unavailable"
    assert linear.read_at is None


def test_data_health_generated_at_and_published_at_are_distinct() -> None:
    """The projection knows when it generated the record; the publish node stamps when it published it."""
    result = compute_factory_floor(request())
    health = result.data_health
    assert health.generated_at == result.generated_at == "2026-10-31T12:00:00Z"
    assert health.published_at is None
    dumped = result.model_dump(mode="json")["data_health"]
    assert dumped["published_at"] is None
    assert dumped["generated_at"] != dumped["published_at"]
