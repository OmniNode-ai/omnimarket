# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18768 AC2/AC4 — the pure runner-fleet derivation.

These pin the reducer half of the ticket: given one fleet observation and the
runner names this host already has materialized, what rows does the projection
own and which keys does it owe a tombstone.

The two properties that carry the ticket:

  * an OFFLINE runner is materialized offline, never dropped (AC4). Dropping it
    makes a fleet outage render as a smaller, entirely healthy fleet — which is
    the false-green this whole epic exists to close.
  * a runner that DISAPPEARED is tombstoned, never left behind (AC2). A
    deregistered runner that keeps reporting `online` is worse than no row,
    because a capacity panel counts it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_projection_runner_fleet import (
    HandlerProjectionRunnerFleet,
)
from omnimarket.nodes.node_projection_runner_fleet.models import (
    EnumRunnerStatus,
    ModelRunnerFleetObservationWire,
    ModelRunnerFleetProjectionRequest,
)

OBSERVED = datetime(2026, 9, 18, 23, 30, 0, tzinfo=UTC)
HOST = "omni-201"

pytestmark = pytest.mark.unit


def _runner(
    name: str,
    *,
    status: str = "online",
    label_class: str = "omnibase-ci",
    job: str | None = None,
    runner_id: int | None = 1,
) -> dict[str, object]:
    return {
        "runner_name": name,
        "runner_id": runner_id,
        "label_class": label_class,
        "labels": ["self-hosted", label_class],
        "host": HOST,
        "status": status,
        "current_job_id": job,
        "observed_at": OBSERVED.isoformat(),
    }


def _observe(runners: list[dict[str, object]], *, host: str = HOST):
    return ModelRunnerFleetObservationWire.model_validate(
        {
            "schema_version": "1.0.0",
            "event_type": "runner-fleet-observation",
            "host": host,
            "observed_at": OBSERVED.isoformat(),
            "runners": runners,
        }
    )


def _derive(runners: list[dict[str, object]], known: tuple[str, ...] = ()):
    return HandlerProjectionRunnerFleet().handle(
        ModelRunnerFleetProjectionRequest(
            observation=_observe(runners), known_runner_names=known
        )
    )


def test_every_observed_runner_becomes_a_keyed_row() -> None:
    """AC2 — one row per runner, keyed on runner name."""
    result = _derive(
        [_runner(f"omninode-runner-{i}", runner_id=i) for i in range(1, 70)]
    )
    assert len(result.rows) == 69
    assert len({row.runner_name for row in result.rows}) == 69
    assert result.rows[0].label_class == "omnibase-ci"
    assert result.rows[0].host == HOST
    assert result.rows[0].observed_at == OBSERVED


def test_an_offline_runner_is_materialized_offline_not_dropped() -> None:
    """AC4 — an offline runner is reported offline rather than omitted."""
    result = _derive(
        [
            _runner("omninode-runner-1", runner_id=1),
            _runner("omninode-runner-2", status="offline", runner_id=2),
        ]
    )
    by_name = {row.runner_name: row for row in result.rows}
    assert by_name["omninode-runner-2"].status is EnumRunnerStatus.OFFLINE
    assert len(result.rows) == 2


def test_an_entirely_offline_fleet_is_a_full_row_set() -> None:
    """The failure this guards: an outage rendering as a smaller healthy fleet."""
    result = _derive(
        [
            _runner(f"omninode-runner-{i}", status="offline", runner_id=i)
            for i in range(1, 6)
        ]
    )
    assert len(result.rows) == 5
    assert all(row.status is EnumRunnerStatus.OFFLINE for row in result.rows)
    assert result.tombstoned_runner_names == ()


def test_a_disappeared_runner_is_tombstoned_not_left_online() -> None:
    """AC2 — the snapshot stream is tombstone-capable.

    The emitter publishes the WHOLE fleet every cycle, which is what makes an
    absence meaningful at all.
    """
    result = _derive(
        [_runner("omninode-runner-1", runner_id=1)],
        known=("omninode-runner-1", "omninode-runner-2", "omninode-runner-3"),
    )
    assert [row.runner_name for row in result.rows] == ["omninode-runner-1"]
    assert result.tombstoned_runner_names == (
        "omninode-runner-2",
        "omninode-runner-3",
    )


def test_a_runner_this_host_never_saw_is_not_tombstoned() -> None:
    """A name absent from BOTH the observation and the known set is not ours."""
    result = _derive([_runner("omninode-runner-1")], known=("omninode-runner-1",))
    assert result.tombstoned_runner_names == ()


def test_a_busy_runner_keeps_its_job_id_and_an_idle_one_never_gains_one() -> None:
    """A job id on an idle runner is a contradiction, not a fact to carry."""
    result = _derive(
        [
            _runner("omninode-runner-1", status="busy", job="4242", runner_id=1),
            _runner("omninode-runner-2", status="online", job="9999", runner_id=2),
            _runner("omninode-runner-3", status="offline", job="7777", runner_id=3),
        ]
    )
    by_name = {row.runner_name: row for row in result.rows}
    assert by_name["omninode-runner-1"].current_job_id == "4242"
    assert by_name["omninode-runner-2"].current_job_id is None
    assert by_name["omninode-runner-3"].current_job_id is None


def test_an_unresolved_job_on_a_busy_runner_stays_null() -> None:
    """NULL on a busy runner means "executing something we could not name" —
    a different fact from idle, and from a fabricated id."""
    result = _derive([_runner("omninode-runner-1", status="busy", job=None)])
    assert result.rows[0].status is EnumRunnerStatus.BUSY
    assert result.rows[0].current_job_id is None


def test_the_row_carries_both_the_runners_host_and_the_observers() -> None:
    """Two host facts, never one.

    Measured live 2026-09-18: the only offline runner in the whole org pool was
    `omninode-air-runner-1`, labelled `host-105`, observed from .201.
    Collapsing these onto the observer points an operator at the wrong machine;
    collapsing them onto the runner loses the tombstone scope.
    """
    runner = _runner("omninode-air-runner-1", status="offline")
    runner["host"] = "host-105"
    runner["observing_host"] = HOST
    result = HandlerProjectionRunnerFleet().handle(
        ModelRunnerFleetProjectionRequest(observation=_observe([runner], host=HOST))
    )
    assert result.rows[0].host == "host-105"
    assert result.rows[0].observing_host == HOST


def test_a_runner_with_no_observing_host_falls_back_to_the_observation() -> None:
    """An emitter that omits the per-runner field still yields a usable row."""
    runner = _runner("omninode-runner-1")
    runner.pop("observing_host", None)
    result = HandlerProjectionRunnerFleet().handle(
        ModelRunnerFleetProjectionRequest(observation=_observe([runner], host=HOST))
    )
    assert result.rows[0].observing_host == HOST


def test_replaying_the_same_observation_derives_identical_rows() -> None:
    """Replay determinism: the same request derives the same result, byte for
    byte, not merely one that means the same thing."""
    runners = [_runner(f"omninode-runner-{i}", runner_id=i) for i in (3, 1, 2)]
    first = _derive(runners)
    second = _derive(list(reversed(runners)))
    assert first == second
    assert [row.runner_name for row in first.rows] == [
        "omninode-runner-1",
        "omninode-runner-2",
        "omninode-runner-3",
    ]


def test_the_class_rollup_counts_each_class_separately() -> None:
    """A fleet-wide healthy count hides a total outage of a single-runner class,
    and the classes are not interchangeable."""
    result = _derive(
        [
            _runner("omninode-runner-1", runner_id=1),
            _runner("omninode-runner-2", status="busy", runner_id=2),
            _runner(
                "omninode-prod-deploy-runner-1",
                status="offline",
                label_class="omnibase-prod-deploy",
                runner_id=3,
            ),
        ]
    )
    rollup = {entry.label_class: entry for entry in result.class_rollup}
    assert rollup["omnibase-ci"].total == 2
    assert rollup["omnibase-ci"].online == 2  # busy runners are up and working
    assert rollup["omnibase-ci"].busy == 1
    assert rollup["omnibase-ci"].offline == 0
    assert rollup["omnibase-prod-deploy"].total == 1
    assert rollup["omnibase-prod-deploy"].offline == 1
    assert rollup["omnibase-prod-deploy"].online == 0


def test_an_empty_observation_derives_no_rows_and_tombstones_everything_known() -> None:
    """A fleet observed as empty is a real, publishable fact — the emitter
    refuses to publish an UNREADABLE fleet, which is the case that must never
    reach here."""
    result = _derive([], known=("omninode-runner-1",))
    assert result.rows == ()
    assert result.tombstoned_runner_names == ("omninode-runner-1",)


def test_an_unknown_status_is_refused_rather_than_coerced() -> None:
    """A status the schema does not know is a producer defect, and coercing it
    to `offline` would page an operator for a rename."""
    with pytest.raises(ValidationError, match="status"):
        _derive([_runner("omninode-runner-1", status="Online")])
