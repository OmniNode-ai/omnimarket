# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot: a host that cannot be reached reads zero free slots and names its reason (OMN-23725).

Golden: a host that advertises nothing and a host whose token pool read failed each read zero free slots with a reason
in the per-host row and in the pool reading the lane start and the fill read.
Failing control: the same host with its advertisement and its accounts read has free slots, so the zero comes from the
unreachable state and not from the inputs' shape.
"""

from __future__ import annotations

import pytest

from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    accounts,
    advertisement,
    handler,
    host_row,
    pool_request,
)

pytestmark = pytest.mark.unit


def _by_host(result):
    assert result.record is not None
    return {row.host: row for row in result.record.hosts}


def test_an_unreachable_host_reads_zero_free_slots_and_names_its_reason() -> None:
    """h2 advertises nothing and its token pool read failed: it is unreachable."""
    result = handler(
        ads=(advertisement("h1"),),
        rows=(host_row("h1"), host_row("h2")),
        lanes={"h1": 1, "h2": 0},
        accs=(
            accounts("h1"),
            accounts("h2", usable=(), unread_reason="ssh: connection timed out"),
        ),
    ).handle(pool_request())
    rows = _by_host(result)
    assert rows["h1"].refusal is None
    assert rows["h1"].free_slots == 3
    assert rows["h2"].free_slots == 0
    assert rows["h2"].refusal == "no-advertisement"
    assert result.record is not None
    assert result.record.free_slots_total == 3


def test_an_unreachable_host_is_a_zero_slot_reading_with_its_reason_for_the_lane_start() -> (
    None
):
    """The reading the start and the fill read carries the same zero and the same reason."""
    h = handler(
        ads=(advertisement("h1"),),
        rows=(host_row("h1"), host_row("h2")),
        accs=(accounts("h1"), accounts("h2", usable=())),
    )
    h.handle(pool_request())
    snapshot = h._store.snapshot
    assert snapshot is not None
    reading = next(r for r in snapshot.readings if r.name == "h2")
    assert reading.lane_slots == 0
    assert reading.lane_admission_refusal == "no-advertisement"


def test_a_host_whose_token_pool_cannot_be_read_reads_zero_free_slots_and_names_why() -> (
    None
):
    """h2 advertises, but ssh to it fails: its accounts are unread and the reason rides in the row."""
    result = handler(
        ads=(advertisement("h1"), advertisement("h2")),
        rows=(host_row("h1"), host_row("h2")),
        accs=(
            accounts("h1"),
            accounts("h2", usable=(), unread_reason="ssh: connection refused"),
        ),
    ).handle(pool_request())
    rows = _by_host(result)
    assert rows["h2"].free_slots == 0
    assert rows["h2"].refusal == "accounts-unread: ssh: connection refused"


def test_failing_control_a_reachable_host_has_free_slots() -> None:
    """With the advertisement and the accounts read, the same h2 row has free slots: the zero above is the unreachable state."""
    result = handler(
        ads=(advertisement("h1"), advertisement("h2")),
        rows=(host_row("h1"), host_row("h2")),
        accs=(accounts("h1"), accounts("h2")),
    ).handle(pool_request())
    rows = _by_host(result)
    assert rows["h2"].free_slots == 4
    assert rows["h2"].refusal is None
