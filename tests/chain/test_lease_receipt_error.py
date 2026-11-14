# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests for the lease receipt reducer (OMN-24484).

Refusals over the bus, each a refused reply carried back on the refused terminal the
contract names (in memory; the broker-backed profile carries every reply on the
recorded terminal the way the landing exemplar does, the reply body keeping its
status):

* a receipt for a revoked epoch — the chain acquires, revokes, then presents the
  receipt of the epoch the revoke withdrew — is refused ``revoked-epoch``; with the
  lease released after the revoke, the table's revoked-epoch bookkeeping refuses
  alone;
* a receipt for a stale epoch — below the subject's current epoch — is refused
  ``stale-epoch``; a take-over would raise the current epoch, but the landing kind
  does not enable it, so the chain builds the same shape on the test overlay's
  kind: acquire, release, acquire again (one counter, never reused), then the
  first epoch's receipt;
* a run that is not the lease's holder presenting an equal epoch's receipt is
  refused ``holder-run-mismatch``: the holder binds the receipt;
* a release with no evidence is refused ``evidence-required``: an evidence kind
  never releases on the clock, deadline or not;
* a caller the kind's overlay does not name is refused ``caller-not-admitted``
  before anything else of the command is read — even a subject off the kind's key
  pattern does not change the reason.

Each carries a failing control: under a named injected break the same assertion is
red, then green again once the break is gone. No named break reaches the admission
check (it runs before the operation's rules), so that control breaks the expected
reason instead, the way the landing error suite's orchestrator control breaks the
expected path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseTable,
)
from tests.chain.lease_chain import (
    LANDING_KIND,
    MUTATIONS,
    LeaseBus,
    ModelLeaseChainOverlay,
    lease_overlay,
    lease_topics,
    make_command,
    make_lease_bus,
)

pytestmark = pytest.mark.unit

# The profile fixture is collected from this module: naming it here is its use.
__all__ = ["lease_overlay"]

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
SUBJECT = "OmniNode-ai/omnimarket#24484"
HOLDER = "worker:OmniNode-ai/omnimarket#24484"
OTHER_RUN = "worker:OmniNode-ai/other#1"
OUTSIDE_CALLER = "somebody-on-the-bus"
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def assert_refusal(
    reply: ModelLeaseReduceOutput,
    reason: EnumLeaseReason,
    command: ModelLeaseReduceInput,
) -> None:
    """A refusal: the typed reason, no epoch acted on, the input table unchanged."""
    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is reason
    assert reply.epoch is None
    assert reply.table == command.table


def assert_carried_on_contract_topics(
    bus: LeaseBus, overlay: ModelLeaseChainOverlay
) -> None:
    """Every message rode a topic of the contract; in memory the last reply refused."""
    topics = lease_topics()
    assert set(bus.carried) <= set(topics.values())
    if overlay.profile == "inmemory":
        assert bus.carried[-1] == topics["refused"]


# ----------------------------------------------------------- revoked epoch
async def drive_revoked(
    overlay: ModelLeaseChainOverlay, *, released: bool = False
) -> None:
    """acquire, revoke, then the receipt of the epoch the revoke withdrew.

    ``released`` also releases the revoked lease first, so no lease row carries the
    epoch any more and the table's revoked-epoch bookkeeping refuses alone — the
    seam the revoked-not-refused break mutes.
    """
    bus = make_lease_bus(overlay)
    await bus.start()
    try:
        acquired = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                SUBJECT,
                NOW,
                "revoked-acquire",
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
        )
        assert acquired.status is EnumLeaseStatus.RECORDED
        assert acquired.epoch == 1
        revoked = await bus.call(
            make_command(
                EnumLeaseOperation.REVOKE,
                acquired.table,
                SUBJECT,
                NOW + MINUTE,
                "revoked-revoke",
                epoch=acquired.epoch,
            )
        )
        assert revoked.status is EnumLeaseStatus.RECORDED
        assert revoked.table.revoked_epochs == (1,)
        table = revoked.table
        if released:
            let_go = await bus.call(
                make_command(
                    EnumLeaseOperation.RELEASE,
                    table,
                    SUBJECT,
                    NOW + 2 * MINUTE,
                    "revoked-release",
                    epoch=acquired.epoch,
                    evidence=True,
                )
            )
            assert let_go.status is EnumLeaseStatus.RECORDED
            assert let_go.table.leases == ()
            table = let_go.table
        command = make_command(
            EnumLeaseOperation.RECORD_RECEIPT,
            table,
            SUBJECT,
            NOW + 3 * MINUTE,
            "revoked-receipt",
            epoch=acquired.epoch,
            holder_run=HOLDER,
            outcome_ref="merged",
        )
        assert_refusal(await bus.call(command), EnumLeaseReason.REVOKED_EPOCH, command)
        assert_carried_on_contract_topics(bus, overlay)
    finally:
        await bus.stop()


def test_error_receipt_of_a_revoked_epoch_is_refused(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    asyncio.run(drive_revoked(lease_overlay))
    # the lease row gone, the table's revoked-epoch bookkeeping refuses alone
    asyncio.run(drive_revoked(lease_overlay, released=True))


def test_control_the_revoked_epoch_chain_fails_when_nothing_is_revoked(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    with MUTATIONS["revoked-not-refused"](), pytest.raises(AssertionError):
        asyncio.run(drive_revoked(lease_overlay, released=True))
    # and green again once the break is gone
    asyncio.run(drive_revoked(lease_overlay, released=True))


# ------------------------------------------------------------ stale epoch
async def drive_stale(overlay: ModelLeaseChainOverlay) -> None:
    """acquire, release, acquire again (one counter, never reused), then the first
    epoch's receipt: an epoch below the subject's current one."""
    bus = make_lease_bus(overlay)
    await bus.start()
    try:
        first = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                SUBJECT,
                NOW,
                "stale-acquire-first",
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
        )
        assert first.status is EnumLeaseStatus.RECORDED
        let_go = await bus.call(
            make_command(
                EnumLeaseOperation.RELEASE,
                first.table,
                SUBJECT,
                NOW + MINUTE,
                "stale-release",
                epoch=first.epoch,
                evidence=True,
            )
        )
        assert let_go.status is EnumLeaseStatus.RECORDED
        second = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                let_go.table,
                SUBJECT,
                NOW + 2 * MINUTE,
                "stale-acquire-second",
                holder_run=HOLDER,
                deadline_at=NOW + 2 * MINUTE + HOUR,
            )
        )
        assert second.status is EnumLeaseStatus.RECORDED
        # the re-grant sits strictly above the released epoch
        assert (second.epoch, second.table.next_epoch) == (2, 3)
        assert second.table.retained == ()
        command = make_command(
            EnumLeaseOperation.RECORD_RECEIPT,
            second.table,
            SUBJECT,
            NOW + 3 * MINUTE,
            "stale-receipt",
            epoch=first.epoch,
            holder_run=HOLDER,
            outcome_ref="merged",
        )
        assert_refusal(await bus.call(command), EnumLeaseReason.STALE_EPOCH, command)
        assert_carried_on_contract_topics(bus, overlay)
    finally:
        await bus.stop()


def test_error_receipt_of_a_stale_epoch_is_refused(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    asyncio.run(drive_stale(lease_overlay))


def test_control_the_stale_chain_fails_when_the_grant_does_not_raise_the_counter(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    """Without the counter raise the chain cannot build the stale shape at all: the
    grant writes an epoch the table's own shape refuses, and the bus raises."""
    with MUTATIONS["no-epoch-raise"](), pytest.raises(AssertionError):
        asyncio.run(drive_stale(lease_overlay))
    # and green again once the break is gone
    asyncio.run(drive_stale(lease_overlay))


# ------------------------------------------------------- holder binds receipt
async def drive_holder_run(overlay: ModelLeaseChainOverlay) -> None:
    """acquire, then the epoch's receipt presented by a run that is not the holder."""
    bus = make_lease_bus(overlay)
    await bus.start()
    try:
        acquired = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                SUBJECT,
                NOW,
                "holder-acquire",
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
        )
        assert acquired.status is EnumLeaseStatus.RECORDED
        command = make_command(
            EnumLeaseOperation.RECORD_RECEIPT,
            acquired.table,
            SUBJECT,
            NOW + MINUTE,
            "holder-receipt",
            epoch=acquired.epoch,
            holder_run=OTHER_RUN,
            outcome_ref="merged",
        )
        assert_refusal(
            await bus.call(command), EnumLeaseReason.HOLDER_RUN_MISMATCH, command
        )
        assert_carried_on_contract_topics(bus, overlay)
    finally:
        await bus.stop()


def test_error_receipt_from_a_run_that_is_not_the_holder_is_refused(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    asyncio.run(drive_holder_run(lease_overlay))


def test_control_the_holder_run_chain_fails_when_any_run_may_present(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    """With every equal epoch accepted from any run the receipt is recorded and the
    refusal assertion is red."""
    with MUTATIONS["equal-epoch-other-holder"](), pytest.raises(AssertionError):
        asyncio.run(drive_holder_run(lease_overlay))
    # and green again once the break is gone
    asyncio.run(drive_holder_run(lease_overlay))


# ---------------------------------------------------- release needs evidence
async def drive_release_evidence(overlay: ModelLeaseChainOverlay) -> None:
    """acquire, then a release with no evidence past the deadline: an evidence kind
    never releases on the clock."""
    bus = make_lease_bus(overlay)
    await bus.start()
    try:
        acquired = await bus.call(
            make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                SUBJECT,
                NOW,
                "release-acquire",
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
        )
        assert acquired.status is EnumLeaseStatus.RECORDED
        command = make_command(
            EnumLeaseOperation.RELEASE,
            acquired.table,
            SUBJECT,
            NOW + HOUR + MINUTE,
            "release-no-evidence",
            epoch=acquired.epoch,
            evidence=False,
        )
        assert_refusal(
            await bus.call(command), EnumLeaseReason.EVIDENCE_REQUIRED, command
        )
        assert_carried_on_contract_topics(bus, overlay)
    finally:
        await bus.stop()


def test_error_release_without_evidence_is_refused_past_the_deadline(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    asyncio.run(drive_release_evidence(lease_overlay))


def test_control_the_release_chain_fails_when_the_deadline_releases(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    """With an evidence kind releasing on the clock the release is recorded and the
    refusal assertion is red."""
    with MUTATIONS["release-on-deadline"](), pytest.raises(AssertionError):
        asyncio.run(drive_release_evidence(lease_overlay))
    # and green again once the break is gone
    asyncio.run(drive_release_evidence(lease_overlay))


# --------------------------------------------------------- outside caller
async def drive_outside_caller(overlay: ModelLeaseChainOverlay) -> None:
    """an acquire from a caller the kind's overlay does not name, once with a
    subject on the kind's key pattern and once off it: the admission rule is read
    before anything else of the command, so both are refused the same way."""
    bus = make_lease_bus(overlay)
    await bus.start()
    try:
        for correlation, subject in (
            ("outside-on-pattern", SUBJECT),
            ("outside-off-pattern", "not a subject key"),
        ):
            command = make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                subject,
                NOW,
                correlation,
                principal=OUTSIDE_CALLER,
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
            assert_refusal(
                await bus.call(command), EnumLeaseReason.CALLER_NOT_ADMITTED, command
            )
        topics = lease_topics()
        assert set(bus.carried) <= set(topics.values())
        # in memory every refused reply rode the refused terminal
        if overlay.profile == "inmemory":
            assert bus.carried == [topics["command"], topics["refused"]] * 2
    finally:
        await bus.stop()


def test_error_an_outside_caller_is_refused_before_the_command_is_read(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    asyncio.run(drive_outside_caller(lease_overlay))


def test_control_the_outside_caller_chain_fails_when_the_reason_is_wrong(
    lease_overlay: ModelLeaseChainOverlay,
) -> None:
    """No named break reaches the admission check — it runs before the operation's
    rules — so this control breaks the expected reason instead, the way the landing
    error suite's orchestrator control breaks the expected path."""

    async def wrong_reason() -> None:
        bus = make_lease_bus(lease_overlay)
        await bus.start()
        try:
            command = make_command(
                EnumLeaseOperation.ACQUIRE,
                ModelLeaseTable(kind=LANDING_KIND),
                SUBJECT,
                NOW,
                "outside-control",
                principal=OUTSIDE_CALLER,
                holder_run=HOLDER,
                deadline_at=NOW + HOUR,
            )
            assert_refusal(
                await bus.call(command), EnumLeaseReason.REVOKED_EPOCH, command
            )
        finally:
            await bus.stop()

    with pytest.raises(AssertionError):
        asyncio.run(wrong_reason())
