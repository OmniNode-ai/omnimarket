# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_runner_fleet`` (OMN-18768).

Walks the chain the contract declares, hop by hop, with the live org pool's
own numbers from 2026-09-18:

    onex.evt.omnibase-infra.runner-fleet.v1     (one observation per cycle)
        -> runner_fleet_liveness                 (one row per runner)
        -> onex.snapshot.projection.runner-fleet.v1     (bus-backed exposure)
        -> onex.evt.omnimarket.projection-runner-fleet-applied.v1  (terminal)

The chain's whole purpose is one distinction: a fleet of 69 with one runner
down must not look the same as a healthy fleet of 68. Everything else here is
in service of that.

Before this chain existed there was no hop at all. A sweep of every
``onex.snapshot.projection.*`` topic across the runtime sources returned 60+
topics and not one runner, lane, fleet or host topic (OMN-16943: the runner
monitor knew the answer on a 3-minute cadence and told a chat channel).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_projection_runner_fleet import (
    HandlerProjectionRunnerFleet,
)
from omnimarket.nodes.node_projection_runner_fleet.models import (
    EnumRunnerStatus,
    ModelRunnerFleetObservationWire,
    ModelRunnerFleetProjectionRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runner_fleet"
    / "contract.yaml"
)

_OBSERVATION_TOPIC = "onex.evt.omnibase-infra.runner-fleet.v1"  # onex-topic-allow: the emitter's declared topic, omnibase_infra side of OMN-18768
_SNAPSHOT_TOPIC = "onex.snapshot.projection.runner-fleet.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-runner-fleet-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-runner-fleet-malformed.v1"  # onex-topic-allow: this node's declared DLQ

_T0 = datetime(2026, 9, 18, 23, 46, 31, tzinfo=UTC)
_OBSERVER = "omni-201"


def _contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _runner(
    name: str,
    *,
    status: str,
    label_class: str,
    host: str,
    busy_job: str | None = None,
) -> dict[str, Any]:
    return {
        "runner_name": name,
        "runner_id": abs(hash(name)) % 1_000_000,
        "label_class": label_class,
        "labels": ["self-hosted", "Linux", label_class],
        "host": host,
        "observing_host": _OBSERVER,
        "status": status,
        "current_job_id": busy_job,
        "observed_at": _T0.isoformat(),
    }


def _live_pool_observation() -> dict[str, Any]:
    """A reduced but faithful shape of the 2026-09-18 org pool.

    Five general-pool runners stand in for the 60 real ones (the count is not
    what this chain is about); every out-of-pool class is present exactly as
    the live pool carries it, including the single offline runner on .105.
    """
    runners = [
        _runner(
            f"omninode-runner-{i}",
            status="busy" if i <= 2 else "online",
            label_class="omnibase-ci",
            host=_OBSERVER,
        )
        for i in range(1, 6)
    ]
    runners += [
        _runner(
            "omninode-air-runner-1",
            status="offline",
            label_class="omnibase-verify",
            host="host-105",
        ),
        _runner(
            "omninode-verify-runner-1",
            status="online",
            label_class="omnibase-verify",
            host="host-201",
        ),
        _runner(
            "omninode-prod-deploy-runner-1",
            status="online",
            label_class="omnibase-prod-deploy",
            host=_OBSERVER,
        ),
        _runner(
            "omninode-customer-plane-runner-1",
            status="online",
            label_class="omnibase-customer-plane",
            host=_OBSERVER,
        ),
    ]
    return {
        "schema_version": "1.0.0",
        "event_type": "runner-fleet-observation",
        "host": _OBSERVER,
        "observed_at": _T0.isoformat(),
        "runners": runners,
    }


@pytest.mark.unit
def test_golden_chain_hops_are_the_declared_topics() -> None:
    """Every hop is contract-declared — none is a code constant.

    A topic that lives only in Python is a topic the platform cannot reason
    about, and this node exists precisely because the platform could not
    reason about its own CI fleet.
    """
    contract = _contract()
    event_bus = contract["event_bus"]

    assert event_bus["subscribe_topics"] == [_OBSERVATION_TOPIC], (
        "the chain must ride the topic the runner monitor emits; a rename on "
        "either side is silent — the reducer simply never receives anything"
    )
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["externally_consumed_topics"] == [_TERMINAL_TOPIC]


@pytest.mark.unit
def test_golden_chain_end_to_end_separates_a_degraded_fleet_from_a_smaller_one() -> (
    None
):
    """The chain, walked with the live pool's own shape.

    A fleet of nine with one runner down must not read as a healthy fleet of
    eight. That is the entire distinction the chain carries, and dropping the
    offline row is the one way to lose it.
    """
    observation = ModelRunnerFleetObservationWire.model_validate(
        _live_pool_observation()
    )
    result = HandlerProjectionRunnerFleet().handle(
        ModelRunnerFleetProjectionRequest(observation=observation)
    )

    assert len(result.rows) == 9
    offline = [r for r in result.rows if r.status is EnumRunnerStatus.OFFLINE]
    assert [r.runner_name for r in offline] == ["omninode-air-runner-1"]
    # And it points at the machine it actually lives on, not the observer.
    assert offline[0].host == "host-105"
    assert offline[0].observing_host == _OBSERVER

    rollup = {entry.label_class: entry for entry in result.class_rollup}
    # The single-runner class that is DOWN is visible as a class, not averaged
    # into a fleet-wide 8-of-9.
    assert rollup["omnibase-verify"].total == 2
    assert rollup["omnibase-verify"].offline == 1
    assert rollup["omnibase-prod-deploy"].offline == 0
    assert rollup["omnibase-ci"].busy == 2


@pytest.mark.unit
def test_golden_chain_second_cycle_tombstones_a_deregistered_runner() -> None:
    """Hop four: a runner that disappears is DELETED, not left reporting online.

    The emitter publishes the whole fleet every cycle, which is what makes an
    absence meaningful at all.
    """
    handler = HandlerProjectionRunnerFleet()
    first = handler.handle(
        ModelRunnerFleetProjectionRequest(
            observation=ModelRunnerFleetObservationWire.model_validate(
                _live_pool_observation()
            )
        )
    )
    known = tuple(row.runner_name for row in first.rows)

    payload = _live_pool_observation()
    payload["runners"] = [
        r for r in payload["runners"] if r["runner_name"] != "omninode-air-runner-1"
    ]
    second = handler.handle(
        ModelRunnerFleetProjectionRequest(
            observation=ModelRunnerFleetObservationWire.model_validate(payload),
            known_runner_names=known,
        )
    )

    assert len(second.rows) == 8
    assert second.tombstoned_runner_names == ("omninode-air-runner-1",)


@pytest.mark.unit
def test_golden_chain_terminal_event_is_the_applied_topic() -> None:
    """The terminal hop is a declared topic, and the writer's return value is
    the payload published on it — not a bare ack, which would discard every
    fleet fact at the producer."""
    from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_fleet_liveness_writer import (
        FleetLivenessProjectionWriter,
    )

    contract = _contract()
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    routed = {entry["operation"] for entry in contract["handler_routing"]["handlers"]}
    assert routed == {"projection_runner_fleet", "fleet_liveness_projection_writer"}
    # The writer is the half the runtime dispatches per message; the pure
    # reducer is the half the def-B contract names as its input model.
    assert FleetLivenessProjectionWriter.onex_runtime_inprocess_dispatch is True
