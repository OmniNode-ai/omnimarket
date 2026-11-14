# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test for the lease receipt reducer (OMN-24484).

An ``acquire`` sent over the bus returns a recorded event; the receipt of that epoch and the release
with evidence follow, each its own command and each its own recorded reply, every message on a topic
the node's contract declares. It runs on the in-memory bus always and on the lab Redpanda dev lane
when ``LEASE_CHAIN_LANE`` (or ``LEASE_CHAIN_BOOTSTRAP``) names a broker.

The control runs the same chain under a named injected break (the grant does not raise the counter)
and requires the chain to fail.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseStatus,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_reduce import (
    ModelLeaseReduceOutput,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseTable,
)
from tests.chain.lease_chain import (
    LANDING_KIND,
    MUTATIONS,
    ModelLeaseChainOverlay,
    injected_break,  # noqa: F401  (autouse fixture, collected from this module)
    lease_overlay,  # noqa: F401  (the profile fixture, collected from this module)
    lease_topics,
    make_command,
    make_lease_bus,
    write_receipt,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
SUBJECT = "OmniNode-ai/omnimarket#24484"
HOLDER = "worker:OmniNode-ai/omnimarket#24484"


async def drive_golden(
    overlay: ModelLeaseChainOverlay, receipt_dir: Path
) -> list[ModelLeaseReduceOutput]:
    """acquire, then the receipt of that epoch, then the release with evidence."""
    bus = make_lease_bus(overlay)
    await bus.start()
    results: list[ModelLeaseReduceOutput] = []
    try:
        table = ModelLeaseTable(kind=LANDING_KIND)
        acquired = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                table,
                SUBJECT,
                NOW,
                "golden-acquire",
                holder_run=HOLDER,
                deadline_at=NOW + timedelta(hours=1),
            )
        )
        results.append(acquired)
        assert acquired.status is EnumLeaseStatus.RECORDED
        assert acquired.epoch == 1
        assert acquired.table.next_epoch == 2

        recorded = await bus.call(
            make_command(
                EnumLeaseOperation.RECORD_RECEIPT,
                acquired.table,
                SUBJECT,
                NOW + timedelta(minutes=5),
                "golden-receipt",
                epoch=acquired.epoch,
                holder_run=HOLDER,
                outcome_ref="merged",
            )
        )
        results.append(recorded)
        assert recorded.status is EnumLeaseStatus.RECORDED
        assert [(r.subject, r.epoch, r.run) for r in recorded.table.receipts] == [
            (SUBJECT, 1, HOLDER)
        ]

        released = await bus.call(
            make_command(
                EnumLeaseOperation.RELEASE,
                recorded.table,
                SUBJECT,
                NOW + timedelta(minutes=6),
                "golden-release",
                epoch=acquired.epoch,
                evidence=True,
            )
        )
        results.append(released)
        assert released.status is EnumLeaseStatus.RECORDED
        assert released.table.leases == ()
        assert [(r.subject, r.epoch) for r in released.table.retained] == [(SUBJECT, 1)]

        topics = lease_topics()
        assert bus.carried.count(topics["command"]) == 3
        assert set(bus.carried) <= set(topics.values())
        write_receipt(overlay, receipt_dir, case="golden", bus=bus, results=results)
    finally:
        await bus.stop()
    return results


def test_golden_acquire_receipt_release_over_the_bus(
    lease_overlay: ModelLeaseChainOverlay,  # noqa: F811
    tmp_path: Path,
) -> None:
    results = asyncio.run(drive_golden(lease_overlay, tmp_path))

    assert [r.operation for r in results] == [
        EnumLeaseOperation.ACQUIRE,
        EnumLeaseOperation.RECORD_RECEIPT,
        EnumLeaseOperation.RELEASE,
    ]
    assert all(r.status is EnumLeaseStatus.RECORDED for r in results)
    assert list(tmp_path.glob(f"lease-receipt-golden-{lease_overlay.profile}-*.json"))


def test_control_the_golden_chain_fails_when_the_grant_does_not_raise_the_counter(
    lease_overlay: ModelLeaseChainOverlay,  # noqa: F811
    tmp_path: Path,
) -> None:
    with MUTATIONS["no-epoch-raise"](), pytest.raises(AssertionError):
        asyncio.run(drive_golden(lease_overlay, tmp_path))
