# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18198: a tenant minted seconds ago must not be quarantined for it.

WHAT WAS MEASURED, so these tests assert against a number the lane produced
rather than one somebody chose. ``tenant_registry_mirror`` is fed by an
event-driven Kafka consumer (``node_projection_tenant_registry`` subscribing
``onex.tenant.events``), not by a poll. Over the 28 tenants minted on onex-dev
since that node went live, ``observed_at - registry_created_at`` was 1.06 s at
best, 3.18 s on average and 8.78 s at worst. The seven rows predating the node
show up to 20 days and are the OMN-17446 historical backfill; excluding them is
what separates the two populations, and they are the control that the 8.78 s is
a real steady-state worst case rather than an artefact.

So the feed is neither slow nor periodic. The C19 tenant that was quarantined
was minted at 18:01:42.487 and mirrored at 18:01:51.267 -- the slowest of the
28 -- and its delegation terminal was refused at 18:01:51, in the same second
the row landed. The mint and the delegation raced.

THE DISCRIMINATOR IS THE WATERMARK, AND THESE TESTS ARE ABOUT KEEPING THE TWO
REFUSALS APART. "The mirror has not caught up to this event yet" and "nobody
ever minted this tenant" look identical at the point of refusal and need
opposite treatments: the first must wait, the second must quarantine at once.
Treating them alike in either direction is a defect -- quarantining the first is
the bug this closes, and waiting on the second would stall every unknown tenant
for the full window, which is how a guard like this wedges a partition (the
failure OMN-17985 reclassified these refusals to avoid).
"""

from __future__ import annotations

import typing as t
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from omnimarket.projection import tenant_registry_resolution as resolution
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
    async_resolve_write_tenant_uuid,
)

pytestmark = pytest.mark.unit

#: The moment the racing event was produced.
EVENT_AT = datetime(2026, 9, 11, 18, 1, 51, tzinfo=UTC)
#: The mirror's watermark at that instant on the live lane: 3 seconds behind.
WATERMARK_BEHIND = EVENT_AT - timedelta(seconds=3)
#: A mirror that has already processed everything up to the event.
WATERMARK_CAUGHT_UP = EVENT_AT + timedelta(seconds=1)


class _FakeDb:
    """A mirror reader that answers the two queries the resolver makes.

    ``rows_after`` is how many row lookups must happen before the tenant
    appears, which is how a mint landing mid-wait is expressed.
    """

    def __init__(
        self,
        *,
        tenant_uuid: UUID,
        watermark: datetime | None,
        rows_after: int,
    ) -> None:
        self.tenant_uuid = tenant_uuid
        self.watermark = watermark
        self.rows_after = rows_after
        self.row_lookups = 0
        self.watermark_reads = 0
        self.sleeps: list[float] = []

    async def fetchval(self, sql: str, *args: object) -> object:
        if "max(observed_at)" in sql:
            self.watermark_reads += 1
            return self.watermark
        self.row_lookups += 1
        if self.row_lookups > self.rows_after:
            return self.tenant_uuid
        return None


@pytest.fixture
def quick_window(monkeypatch: pytest.MonkeyPatch) -> _FakeDb | None:
    """Shrink the real window so the tests do not spend 20 seconds proving it.

    The CONSTANTS are asserted separately, against the measurement, so shrinking
    them here cannot hide a window that was silently set to zero in the source.
    """
    monkeypatch.setattr(resolution, "MIRROR_CATCHUP_POLL_SECONDS", 0.001)
    monkeypatch.setattr(resolution, "MIRROR_CATCHUP_DEADLINE_SECONDS", 0.05)
    return None


async def _sleepless(monkeypatch: pytest.MonkeyPatch, db: _FakeDb) -> None:
    async def _record(seconds: float) -> None:
        db.sleeps.append(seconds)

    monkeypatch.setattr(resolution.asyncio, "sleep", _record)


@pytest.mark.asyncio
async def test_a_mint_newer_than_the_watermark_parks_and_then_resolves(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """GREEN half. The row lands mid-wait and the event is attributed.

    This is the C19 case exactly: the mirror is behind the event, the tenant
    genuinely exists, and the row appears within the window. Before this fix
    the resolver refused on the first miss and the runner quarantined a real
    customer's terminal.
    """
    tenant = uuid4()
    db = _FakeDb(tenant_uuid=tenant, watermark=WATERMARK_BEHIND, rows_after=2)
    await _sleepless(monkeypatch, db)

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=EVENT_AT
    )

    assert resolved == str(tenant)
    assert db.row_lookups > 1, "it must have re-read the mirror, not guessed"
    assert db.sleeps, "a park that never waits is not a park"


@pytest.mark.asyncio
async def test_a_caught_up_mirror_refuses_at_once_and_never_waits(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """RED half, and the one that keeps the fix from wedging a partition.

    When the watermark is at or past the event, the mirror has already
    processed every tenant event up to that moment. A tenant it does not hold
    was never minted, and waiting cannot change that. Stalling here would make
    every genuinely unknown tenant pay the full window -- which is the failure
    mode, not the fix.
    """
    absent = uuid4()
    db = _FakeDb(tenant_uuid=absent, watermark=WATERMARK_CAUGHT_UP, rows_after=99)
    await _sleepless(monkeypatch, db)

    with pytest.raises(TenantRegistryResolutionError) as caught:
        await async_resolve_write_tenant_uuid(db, str(absent), event_timestamp=EVENT_AT)

    assert not db.sleeps, "a caught-up mirror must not be waited on"
    assert db.row_lookups == 1, "one lookup, one refusal"
    assert "OMN-16831" in str(caught.value), (
        "a caught-up mirror yields the UNKNOWN-TENANT refusal, not the lag one"
    )


@pytest.mark.asyncio
async def test_a_lag_that_outlasts_the_window_says_so_and_carries_the_watermark(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """The dead-letter must distinguish the two refusals, not just refuse.

    A quarantined record reading "no row for this tenant" is consistent with
    both "nobody minted it" and "the feed is broken", and those need different
    people to act. The watermark and the event's own timestamp are what tell
    them apart, so they go in the message.
    """
    tenant = uuid4()
    db = _FakeDb(tenant_uuid=tenant, watermark=WATERMARK_BEHIND, rows_after=10**6)
    await _sleepless(monkeypatch, db)

    with pytest.raises(TenantRegistryResolutionError) as caught:
        await async_resolve_write_tenant_uuid(db, str(tenant), event_timestamp=EVENT_AT)

    message = str(caught.value)
    assert "OMN-18198" in message
    assert WATERMARK_BEHIND.isoformat() in message, "the watermark must be named"
    assert EVENT_AT.isoformat() in message, "so must the event it is behind"
    assert "is NOT the unknown-tenant refusal" in message
    assert db.sleeps, "the window must actually have been waited out"


@pytest.mark.asyncio
async def test_a_mirrored_tenant_resolves_without_reading_the_watermark(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """Positive control. The ordinary path must be untouched by all of this.

    A tenant the mirror already holds resolves on the first lookup, with no
    watermark read and no wait -- so the fix costs the overwhelmingly common
    case nothing.
    """
    tenant = uuid4()
    db = _FakeDb(tenant_uuid=tenant, watermark=WATERMARK_BEHIND, rows_after=0)
    await _sleepless(monkeypatch, db)

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=EVENT_AT
    )

    assert resolved == str(tenant)
    assert db.watermark_reads == 0
    assert not db.sleeps


@pytest.mark.asyncio
async def test_a_caller_that_passes_no_timestamp_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """The wait is opt-in, and the paths that did not opt in must not change.

    The probe and verdict call sites pass no timestamp. They must take the same
    immediate refusal they took before this change: no watermark read, no wait.
    A default-on wait would quietly add latency to paths nobody examined.
    """
    absent = uuid4()
    db = _FakeDb(tenant_uuid=absent, watermark=WATERMARK_BEHIND, rows_after=99)
    await _sleepless(monkeypatch, db)

    with pytest.raises(TenantRegistryResolutionError) as caught:
        await async_resolve_write_tenant_uuid(db, str(absent))

    assert db.watermark_reads == 0
    assert not db.sleeps
    assert "OMN-16831" in str(caught.value)


@pytest.mark.asyncio
async def test_an_unanswerable_watermark_does_not_license_the_harsher_outcome(
    monkeypatch: pytest.MonkeyPatch, quick_window: None
) -> None:
    """An empty or unreadable mirror is not proof that the mirror is caught up.

    ``None`` means the question could not be answered. Treating it as "caught
    up" would turn every unanswerable read into an immediate quarantine, which
    is an absence of evidence being read as evidence of absence.
    """
    tenant = uuid4()
    db = _FakeDb(tenant_uuid=tenant, watermark=None, rows_after=1)
    await _sleepless(monkeypatch, db)

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=EVENT_AT
    )

    assert resolved == str(tenant)
    assert db.sleeps, "an unknown watermark must still allow the window"


def test_the_window_is_bounded_by_the_measured_worst_case() -> None:
    """The constants must stay tied to the measurement that justified them.

    Measured worst case on onex-dev: 8.78 s. A window at or below that would
    refuse the very race it exists to absorb; an unbounded one is a wedged
    partition. The assertion is a range, not an equality, so tuning stays
    possible and zeroing it does not.
    """
    assert resolution.MIRROR_CATCHUP_DEADLINE_SECONDS > 8.78
    assert resolution.MIRROR_CATCHUP_DEADLINE_SECONDS <= 60.0
    assert 0 < resolution.MIRROR_CATCHUP_POLL_SECONDS <= 2.0


def test_the_lag_refusal_is_still_poison_so_it_cannot_wedge_a_partition() -> None:
    """OMN-17985 put these refusals in the poison set deliberately. Keep them.

    The fix must not reach the point of raising more often than before, and
    when it does raise the classification must be unchanged: a bounded wait
    followed by a quarantine, never an unbounded retry. This asserts the new
    refusal is the same exception type the classifier already routes to the
    dead letter.
    """
    from omnimarket.projection.error_classification import (
        ProjectionErrorClass,
        classify_projection_error,
    )

    lagged = TenantRegistryResolutionError("OMN-18198: still behind")
    assert classify_projection_error(lagged) is ProjectionErrorClass.POISON


def test_only_the_terminal_write_paths_opt_into_the_wait() -> None:
    """Threading is deliberate and narrow; assert it rather than trusting it.

    The two paths that stamp ``delegation_events.tenant_id`` for a real
    terminal are the ones a freshly minted tenant races. The probe and verdict
    paths pass nothing on purpose. A later edit that made the wait universal
    would add latency to paths nobody measured, so the count is pinned.
    """
    from pathlib import Path

    handler = (
        Path(resolution.__file__).resolve().parents[1]
        / "nodes"
        / "node_projection_delegation"
        / "handlers"
        / "handler_delegation.py"
    )
    source = handler.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert code.count("event_timestamp=") == 3, (
        "two terminal call sites plus the one pass-through in "
        "_resolve_write_tenant_uuid; a third call site opting in is a "
        "deliberate decision, not a drive-by"
    )


def test_the_resolver_module_exports_the_watermark_reader() -> None:
    """The watermark is now part of this module's contract, not a private hack."""
    assert "async_registry_mirror_watermark" in resolution.__all__
    assert "MIRROR_CATCHUP_DEADLINE_SECONDS" in resolution.__all__
    assert t.TYPE_CHECKING or callable(resolution.async_registry_mirror_watermark)
