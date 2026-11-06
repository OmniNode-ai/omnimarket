# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: an event older than the catch-up window is not waited for.

Read on the dev lane on 2026-11-06: the delegation writer waited 20 s for each of
128 events an hour from three tenants that are in no dev registry, about 43 minutes
of every hour. The wait comes from ``tenant_registry_resolution``: the mirror's
newest ``observed_at`` is behind the event, so the watermark test never says
"caught up", and a quiet registry keeps it behind every event.

The wait exists for a mint in flight when the event was produced (OMN-18198), and
the mirror lands such a mint within 8.78 s at worst. An event already older than the
window when it is processed cannot be that race, so it is refused at once. It is not
quarantined silently: it raises the same typed refusal the runner sends to the
dead-letter topic, and the message carries the cause.

The golden chain is the race the wait is for (a fresh event still waits and
resolves). The error chain is the stale event (no wait, a refusal that names its
cause). The control for both is the resolver before this change, which waited for the
stale event too.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from omnimarket.models import tenant_mirror_catchup_policy as catchup_policy
from omnimarket.projection import tenant_registry_resolution as resolution
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
    async_resolve_write_tenant_uuid,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 9, 30, 0, tzinfo=UTC)
#: A quiet registry: its newest observation is two days old.
QUIET_WATERMARK = NOW - timedelta(days=2)


class _FakeDb:
    def __init__(
        self, *, tenant_uuid: UUID, watermark: datetime | None, rows_after: int
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
        return self.tenant_uuid if self.row_lookups > self.rows_after else None


@pytest.fixture(autouse=True)
def _window(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(catchup_policy, "utc_now", lambda: NOW)
    monkeypatch.setattr(resolution, "MIRROR_CATCHUP_POLL_SECONDS", 0.001)
    monkeypatch.setattr(resolution, "MIRROR_CATCHUP_DEADLINE_SECONDS", 20.0)
    monkeypatch.delenv("TENANT_MIRROR_CATCHUP_EVENT_AGE_LIMIT_SECONDS", raising=False)
    catchup_policy.get_catchup_settings.cache_clear()
    yield
    catchup_policy.get_catchup_settings.cache_clear()


async def _sleepless(monkeypatch: pytest.MonkeyPatch, db: _FakeDb) -> None:
    async def _record(seconds: float) -> None:
        db.sleeps.append(seconds)

    monkeypatch.setattr(resolution.asyncio, "sleep", _record)


@pytest.mark.asyncio
async def test_golden_a_fresh_event_inside_the_window_still_waits_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid4()
    produced_at = NOW - timedelta(seconds=3)
    db = _FakeDb(
        tenant_uuid=tenant, watermark=produced_at - timedelta(seconds=2), rows_after=2
    )
    await _sleepless(monkeypatch, db)

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=produced_at
    )

    assert resolved == str(tenant)
    assert db.sleeps, "the race the wait exists for must still be waited out"


@pytest.mark.asyncio
async def test_error_an_event_older_than_the_window_is_refused_without_a_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absent = uuid4()
    produced_at = NOW - timedelta(seconds=45)
    db = _FakeDb(tenant_uuid=absent, watermark=QUIET_WATERMARK, rows_after=10**6)
    await _sleepless(monkeypatch, db)

    with pytest.raises(TenantRegistryResolutionError) as refused:
        await async_resolve_write_tenant_uuid(
            db, str(absent), event_timestamp=produced_at
        )

    assert not db.sleeps, "an event past the window must not be waited for"
    assert db.row_lookups == 1
    message = str(refused.value)
    assert "OMN-22478" in message
    assert "was not waited for" in message
    assert QUIET_WATERMARK.isoformat() in message
    assert produced_at.isoformat() in message
    assert "No identity will be invented or defaulted" in message


@pytest.mark.asyncio
async def test_error_a_stale_event_whose_tenant_the_mirror_holds_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid4()
    db = _FakeDb(tenant_uuid=tenant, watermark=QUIET_WATERMARK, rows_after=0)
    await _sleepless(monkeypatch, db)

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=NOW - timedelta(minutes=10)
    )

    assert resolved == str(tenant)
    assert not db.sleeps
    assert db.watermark_reads == 0


@pytest.mark.asyncio
async def test_the_age_limit_is_set_by_the_overlay_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid4()
    produced_at = NOW - timedelta(seconds=45)
    db = _FakeDb(
        tenant_uuid=tenant, watermark=produced_at - timedelta(seconds=1), rows_after=1
    )
    await _sleepless(monkeypatch, db)
    # The overlay widens the limit past the event's age: the wait applies again.
    monkeypatch.setenv("TENANT_MIRROR_CATCHUP_EVENT_AGE_LIMIT_SECONDS", "120")
    catchup_policy.get_catchup_settings.cache_clear()

    resolved = await async_resolve_write_tenant_uuid(
        db, str(tenant), event_timestamp=produced_at
    )

    assert resolved == str(tenant)
    assert db.sleeps


@pytest.mark.asyncio
async def test_a_zero_age_limit_waits_for_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absent = uuid4()
    produced_at = NOW - timedelta(seconds=1)
    db = _FakeDb(
        tenant_uuid=absent,
        watermark=produced_at - timedelta(seconds=1),
        rows_after=10**6,
    )
    await _sleepless(monkeypatch, db)
    monkeypatch.setenv("TENANT_MIRROR_CATCHUP_EVENT_AGE_LIMIT_SECONDS", "0")
    catchup_policy.get_catchup_settings.cache_clear()

    with pytest.raises(TenantRegistryResolutionError):
        await async_resolve_write_tenant_uuid(
            db, str(absent), event_timestamp=produced_at
        )

    assert not db.sleeps
