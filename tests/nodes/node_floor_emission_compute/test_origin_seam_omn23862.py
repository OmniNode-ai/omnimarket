# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the real-work readiness rule (OMN-23862).

Four pieces share the typed origin of a chain: the emission model (``ModelNodeEmission.origin``, set by ``ChainEmitter``), the
emission node's join fold (``real_golden_at``), the floor compute's ``_integrate`` and the overlay's readiness block
(``real_origins``, ``blocking_neighbour_statuses``). Each lives in its own file; this one measures the agreements (part A), runs
the real fold into the real floor compute end to end (part B), and carries the chain events over a bus, in memory and, when
``FLOOR_ORIGIN_SEAM_BOOTSTRAP`` or ``FLOOR_ORIGIN_SEAM_LANE`` names a lab broker, over Redpanda (part C). Every control is a named
break shown red beside the same check green without it.

The origin and the neighbour reading reach the rule through the receipts and the readings on the input model; nothing here
reads or writes a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import typing
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.floor_projection import BusSnapshotSource
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimpleOverlay,
    ModelJoinReceipt,
    ModelNodeEmission,
)
from omnimarket.models.floor_17a.model_floor_17a import (
    ChainOrigin,
    ModelFloorJoinRow,
    ModelReadinessOverlay,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from tests.nodes.node_floor_simple_compute.test_readiness_omn23081 import (
    COUNTED,
    NOW,
    overlay,
)
from tests.nodes.node_floor_simple_compute.test_real_work_omn23862 import board, ready
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "FLOOR_ORIGIN_SEAM_BOOTSTRAP"
LANE_ENV = "FLOOR_ORIGIN_SEAM_LANE"
EMISSION_TOPIC = "onex.evt.omnimarket.floor-node-succeeded.v1"
OVERLAY = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
)
JOINS = {j.id: j for j in OVERLAY.joins}
START = NOW - timedelta(hours=2)


def chains_of_every_counted_join(
    origin: ChainOrigin | None, run: str = "seam"
) -> list[ModelNodeEmission]:
    """A golden and an error chain for each counted join, emitted through the real ``ChainEmitter``."""
    sink = InMemoryEmissionSink()
    tick = iter(range(10_000))

    def clock() -> datetime:
        return START + timedelta(seconds=next(tick))

    def emitter(join: str, piece: int, kind: str) -> ChainEmitter:
        return ChainEmitter(
            sink,
            node=f"node_seam_{piece}:run",
            piece=piece,
            clock=clock,
            correlation_id=f"{run}-{join}-{kind}",
            origin=origin,
        )

    for join in COUNTED:
        a, b = JOINS[join].from_piece, JOINS[join].to_piece
        up, down = emitter(join, a, "golden"), emitter(join, b, "golden")
        up.start()
        up.success()
        down.start()
        down.success()
        up, down = emitter(join, a, "error"), emitter(join, b, "error")
        up.start()
        up.failure("x")
        down.refuse("upstream_x")
    return sink.events


def fold(events: list[ModelNodeEmission], real_origins: tuple[str, ...]):
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=tuple(events),
            pieces=OVERLAY.pieces,
            # J5's judging bound (OMN-23086) is a later instant than these chains; it is not what this file measures.
            joins=tuple(
                j.model_copy(update={"judged_since": None}) for j in OVERLAY.joins
            ),
            freshness_max_age_s=OVERLAY.freshness_max_age_s,
            error_max_age_s=OVERLAY.error_receipt_max_age_s,
            chain_deadline_s=OVERLAY.chain_deadline_s,
            judged_to_pieces=tuple(range(1, 18)),
            real_origins=typing.cast("tuple[ChainOrigin, ...]", real_origins),
        )
    )


# --- part A: the agreements ----------------------------------------------------------------------------------------------


def test_golden_the_vocabularies_and_fields_agree_across_the_four_sides() -> None:
    vocabulary = set(typing.get_args(ChainOrigin))
    assert (
        set(typing.get_args(ModelNodeEmission.model_fields["origin"].annotation))
        == vocabulary
    )
    # The overlay's values are drawn from the emission vocabulary, and the fold input takes the overlay's field by name.
    assert set(OVERLAY.readiness.real_origins) <= vocabulary
    assert "unknown" not in OVERLAY.readiness.real_origins
    assert "real_origins" in ModelFloorReceiptInput.model_fields
    # The fold's join receipt field is the one the floor compute's join row reads.
    assert "real_golden_at" in ModelJoinReceipt.model_fields
    assert {"real_golden_age_s", "real_passes"} <= set(ModelFloorJoinRow.model_fields)
    assert {"real_origins", "blocking_neighbour_statuses"} <= set(
        ModelReadinessOverlay.model_fields
    )


def test_named_break_an_overlay_that_counts_unknown_as_real_is_not_a_rule_change_of_the_emitter() -> (
    None
):
    """The emitter says ``unknown`` when told nothing; only the overlay can make an origin count, and it names its values."""
    unset = chains_of_every_counted_join(None)
    assert {e.origin for e in unset} == {"unknown"}
    counted = fold(unset, real_origins=("unknown",))
    assert all(j.real_golden_at is not None for j in counted.joins if j.join in COUNTED)
    # The shipped overlay does not: the same events are not real work under it.
    shipped = fold(unset, real_origins=OVERLAY.readiness.real_origins)
    assert all(j.real_golden_at is None for j in shipped.joins)


def test_named_break_a_drill_that_names_no_origin_is_a_drill_never_real() -> None:
    sink = InMemoryEmissionSink()
    chain = ChainEmitter(sink, node="n:x", piece=3, drill=True)
    chain.start()
    chain.success()
    assert {e.origin for e in sink.events} == {"drill"}


# --- part B: the real fold into the real floor compute -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expect_ready"),
    [
        ("real", True),
        ("probe", False),
        ("twin", False),
        ("canary", False),
        (None, False),
    ],
)
def test_golden_chain_the_origin_of_the_chains_decides_what_the_board_reads(
    origin: ChainOrigin | None, expect_ready: bool
) -> None:
    receipts = fold(
        chains_of_every_counted_join(origin), OVERLAY.readiness.real_origins
    )
    doc = board(folded=receipts, covered_s=48 * 3600)
    assert bool(ready(doc)) is expect_ready
    assert doc.readiness is not None
    # Probe health is the same whatever the origin: the join passes on chains either way.
    assert doc.readiness.probe_health_joins == COUNTED
    assert doc.readiness.probe_only_joins == (() if expect_ready else COUNTED)
    if not expect_ready:
        assert (
            "green on probes only: no real work crossed these joins in 48 hours (J0, J1)"
            in next(p for p in doc.pieces if p.piece == 1).text
        )


def test_error_chain_a_not_working_neighbour_blocks_real_work_through_the_whole_fold() -> (
    None
):
    receipts = fold(
        chains_of_every_counted_join("real"), OVERLAY.readiness.real_origins
    )
    doc = board(
        folded=receipts,
        working=(1, 2, 4, 5, 6, 8, 9, 17),
        not_working=(3,),
    )
    assert 2 not in ready(doc)
    assert next(p for p in doc.pieces if p.piece == 2).blocking_neighbours == (3,)


def test_named_break_the_overlay_value_decides_through_the_whole_fold() -> None:
    events = chains_of_every_counted_join("probe")
    assert ready(
        board(folded=fold(events, ("probe",)), ov=overlay(real_origins=["probe"]))
    )
    assert not ready(board(folded=fold(events, ("real",))))


# --- part C: the chain events over a bus -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ],
)
async def test_chain_events_keep_their_origin_over_the_bus(backend: str) -> None:
    """A publisher's emission, as the emission topics carry it, reaches the fold with its origin; an origin-less one is unknown."""
    # The broker grants the emission topic itself, not a suffixed copy; every event of this run carries its own token, so what an
    # earlier run or a real publisher left on the topic is not read as this run's.
    run = f"seam-{uuid.uuid4().hex[:8]}"
    topic = EMISSION_TOPIC
    events = chains_of_every_counted_join("real", run)
    legacy = json.loads(events[0].model_dump_json())
    legacy["correlation_id"] = f"{run}-legacy"
    del legacy["origin"]  # a publisher from before the field: it sets nothing

    if backend == "inmemory":
        bus = bind_infra_event_bus(environment="local", group="floor-origin-seam")
        await bus.start()

        @contextlib.asynccontextmanager
        async def open_bus() -> AsyncIterator[Any]:
            yield bus

    else:
        bootstrap = os.environ.get(BOOTSTRAP_ENV)
        lane = os.environ.get(LANE_ENV)
        if not (bootstrap or lane):
            pytest.skip(
                f"Kafka not reachable: {BOOTSTRAP_ENV} and {LANE_ENV} name no lab broker"
            )

        def open_bus() -> Any:
            return open_lane_bus(
                bus="kafka",
                lane=None if bootstrap else lane,
                kafka_bootstrap=bootstrap,
                root=workspace_root(os.environ),
            )

    reader = BusSnapshotSource(
        open_bus=open_bus, group_prefix="floor-origin-seam", window_s=3.0
    )
    listen = asyncio.create_task(reader.publications(topic))
    await asyncio.sleep(0.5)
    async with open_bus() as publisher:
        for event in events:
            await publisher.publish(
                topic,
                event.correlation_id.encode("utf-8"),
                event.model_dump_json().encode("utf-8"),
            )
        await publisher.publish(topic, b"legacy", json.dumps(legacy).encode("utf-8"))
    publications = await listen
    received = [
        e
        for e in (ModelNodeEmission.model_validate_json(p) for p in publications)
        if e.correlation_id.startswith(run)
    ]
    assert len(received) == len(events) + 1
    assert received[:-1] == events
    assert received[-1].origin == "unknown"
    receipts: ModelFloorReceipts = fold(received[:-1], OVERLAY.readiness.real_origins)
    assert all(
        j.real_golden_at is not None for j in receipts.joins if j.join in COUNTED
    )
