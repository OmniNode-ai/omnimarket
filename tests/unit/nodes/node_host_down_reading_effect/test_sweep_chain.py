# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of one sweep, and the readings it publishes (OMN-19998, L0-06).

A seeded unreachable host reads SUSPECT until its probes have failed for the overlay's down bound, then DOWN; a host
that advertised capacity within the bound never reads down; a host that answers again reads UP. Each break the overlay
names is a typed failure on the same chain.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    ModelHostReading,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.stores import (
    InMemoryDownStore,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    EnumSweepOutcome,
    ModelHostDownSweepRequest,
)
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    HostTableError,
)

from .conftest import HOST_A, HOST_B, NOW, Rig

pytestmark = [pytest.mark.unit]


def _sweep(rig: Rig, cid: str = "golden-1"):
    return rig.handler.handle(ModelHostDownSweepRequest(correlation_id=cid))


def test_chain_golden_is_a_start_then_a_success_and_one_reading_per_available_host() -> (
    None
):
    rig = Rig(results={HOST_A: True, HOST_B: True})
    result = _sweep(rig)
    assert result.outcome is EnumSweepOutcome.SWEPT
    assert [(e.phase, e.correlation_id, e.node) for e in rig.sink.events] == [
        ("start", "golden-1", rig.overlay.chain_node),
        ("success", "golden-1", rig.overlay.chain_node),
    ]
    assert {e.piece for e in rig.sink.events} == {rig.overlay.piece}
    # the unavailable row is never probed; the others are probed at their ssh address, user part dropped
    assert rig.probed == [
        f"{HOST_A}@a.example:{rig.overlay.probe.port}",
        f"{HOST_B}@b.example:{rig.overlay.probe.port}",
    ]
    assert [p[1] for p in rig.publisher.published] == [HOST_A, HOST_B]
    assert result.down_hosts == ()


def test_a_seeded_unreachable_host_reads_down_at_the_bound_and_not_before() -> None:
    rig = Rig(results={HOST_A: True, HOST_B: False})
    bound = rig.overlay.rules.down_bound_s
    first = _sweep(rig, "t0")
    assert first.down_hosts == ()
    rig.advance(bound - 1)
    assert _sweep(rig, "t1").down_hosts == ()
    rig.advance(1)
    at_bound = _sweep(rig, "t2")
    assert at_bound.down_hosts == (HOST_B,)
    down = next(r for r in at_bound.readings if r.host_name == HOST_B)
    assert down.down_since == NOW
    assert rig.store.down_hosts(rig.now, rig.overlay.reading_ttl_s) == (HOST_B,)
    published = [
        ModelHostReading.model_validate(json.loads(p[2]))
        for p in rig.publisher.published
        if p[1] == HOST_B
    ]
    assert published[-1].state.value == "down"


def test_the_failing_control_a_host_that_answers_never_reads_down() -> None:
    rig = Rig(results={HOST_A: True, HOST_B: True})
    for _ in range(3):
        assert _sweep(rig).down_hosts == ()
        rig.advance(rig.overlay.rules.down_bound_s)


def test_a_host_that_answers_again_reads_up_and_leaves_the_down_list() -> None:
    rig = Rig(results={HOST_A: True, HOST_B: False})
    _sweep(rig)
    rig.advance(rig.overlay.rules.down_bound_s)
    assert _sweep(rig).down_hosts == (HOST_B,)
    rig.prober._results[HOST_B] = True
    rig.advance(60)
    assert _sweep(rig).down_hosts == ()
    assert rig.store.down_hosts(rig.now, rig.overlay.reading_ttl_s) == ()


def test_a_host_that_advertised_within_the_bound_is_suspect_never_down() -> None:
    rig = Rig(
        results={HOST_A: True, HOST_B: False},
        adverts={HOST_B: NOW + timedelta(seconds=1000)},
    )
    _sweep(rig)
    rig.advance(1000)
    result = _sweep(rig)
    assert result.down_hosts == ()
    assert next(r for r in result.readings if r.host_name == HOST_B).state.value == (
        "suspect"
    )


def test_an_observer_that_sees_no_host_reads_every_host_unknown() -> None:
    rig = Rig(results={HOST_A: False, HOST_B: False})
    _sweep(rig)
    rig.advance(rig.overlay.rules.down_bound_s)
    result = _sweep(rig)
    assert result.down_hosts == ()
    assert {r.state.value for r in result.readings} == {"unknown"}


def _failure(rig: Rig, code: str, cid: str = "err-1"):
    assert [(e.phase, e.correlation_id) for e in rig.sink.events] == [
        ("start", cid),
        ("failure", cid),
    ]
    assert rig.sink.events[-1].failure_code == code


def test_chain_error_an_observer_outside_the_overlay_probes_and_writes_nothing() -> (
    None
):
    rig = Rig(results={HOST_A: True, HOST_B: True}, observer="the-mac")
    result = _sweep(rig, "err-1")
    assert result.outcome is EnumSweepOutcome.FAILED
    assert result.failure_code == rig.overlay.codes.observer_not_allowed
    _failure(rig, rig.overlay.codes.observer_not_allowed)
    assert rig.probed == []
    assert rig.publisher.published == []


def test_chain_error_an_unreadable_host_table_is_a_typed_failure() -> None:
    def unreadable():
        raise HostTableError("the lab host table could not be read")

    rig = Rig(results={}, table_source=unreadable)
    result = _sweep(rig, "err-1")
    assert result.failure_code == rig.overlay.codes.table_unreadable
    _failure(rig, rig.overlay.codes.table_unreadable)


def test_chain_error_a_store_that_refuses_the_write_is_a_typed_failure() -> None:
    class Refusing(InMemoryDownStore):
        def write(self, record) -> None:
            raise OSError("disk full")

    rig = Rig(results={HOST_A: True, HOST_B: True}, store=Refusing())
    result = _sweep(rig, "err-1")
    assert result.failure_code == rig.overlay.codes.write_failed
    _failure(rig, rig.overlay.codes.write_failed)
    assert rig.publisher.published == []


def test_chain_error_the_named_injected_fault_a_store_that_keeps_no_down_reading() -> (
    None
):
    """The overlay's injected fault: the write returns and the readback finds the host not down."""

    class Forgetful(InMemoryDownStore):
        def down_hosts(self, as_of, max_age_s):
            return ()

    rig = Rig(results={HOST_A: True, HOST_B: False}, store=Forgetful())
    _sweep(rig, "seed")
    rig.advance(rig.overlay.rules.down_bound_s)
    rig.sink.events.clear()
    result = _sweep(rig, "err-1")
    assert result.failure_code == rig.overlay.codes.readback_failed
    _failure(rig, rig.overlay.codes.readback_failed)


def test_chain_error_a_bus_that_refuses_a_reading_is_a_typed_failure_and_the_store_keeps_it() -> (
    None
):
    class Refusing:
        def publish(self, topic: str, key: str, payload: bytes) -> None:
            raise RuntimeError("bus refused")

    rig = Rig(results={HOST_A: True, HOST_B: True}, publisher=Refusing())  # type: ignore[arg-type]
    result = _sweep(rig, "err-1")
    assert result.failure_code == rig.overlay.codes.publish_failed
    _failure(rig, rig.overlay.codes.publish_failed)
    assert rig.store.latest(HOST_A) is not None
