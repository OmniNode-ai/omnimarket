# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one read both slot-keyed board steps (the beta board's probes and the MVP launch block's workflow lists) go through.

A scheduled source's list can change only when a cron slot passes and its run finishes, so a read is kept under its slot and served while
that slot's run is current. A late scheduled run leaves the slot's run unfinished for a while; reading the source on every cycle until it
ends cost about 25 s per read per cycle and stored nothing. While the slot is unfinished the step now serves the newest cached read
(marked pending since the slot, carrying its own read time) and goes back to the source at most once per the contract's
``pending_recheck_interval_minutes``. A remembered missing-workflow answer is looked up first and counted apart from hits and misses.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_slot_read import ModelSlotReadEnvelope
from omnimarket.models.model_source_read_cache import ModelSourceCacheKey
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    OWN_CONTRACT,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.schedule_revision import (
    SLOT_STAMP,
    schedule_slot,
)

# The revision under which the newest read of a source is kept, beside the slot-stamped revisions.
LATEST_REVISION = "latest"


@functools.lru_cache(maxsize=8)
def pending_recheck_interval(contract_path: Path = OWN_CONTRACT) -> timedelta:
    """The longest a pending read is served before the source is read again, from the contract's runtime block."""
    runtime = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["runtime"]
    return timedelta(minutes=int(runtime["pending_recheck_interval_minutes"]))


@dataclass(frozen=True)
class SlotReadPlan[T]:
    """What one step says about one source so the shared read can key, judge and mark it."""

    source_id: str
    params: Mapping[str, object]
    crons: Sequence[str]
    # The missing-workflow answer is kept under this id for the repository's default branch commit ``head`` (``None``: not kept).
    absent_source_id: str
    workflow: str
    head: str | None
    fetch: Callable[[], T]
    encode: Callable[[T], str]
    decode: Callable[[str], T]
    # The slot's run has finished and is in the read.
    is_current: Callable[[T, datetime], bool]
    # The read answered (its source could be read), so it may be served pending; an unread answer is never kept.
    is_readable: Callable[[T], bool]
    is_missing: Callable[[T], bool]
    with_read_at: Callable[[T, datetime], T]
    with_pending: Callable[[T, datetime], T]


def read_slot_through_cache[T](
    cache: CachedSourceReader,
    plan: SlotReadPlan[T],
    now: datetime,
    recheck: timedelta | None = None,
) -> T:
    """One source read: the remembered missing answer, the slot's cached read, the pending read, or the source, in that order."""
    if not cache.enabled:
        cache.tally.bypassed += 1
        return cache.fetch_source(plan.fetch)
    unavailable_before = cache.tally.unavailable
    absent_key = (
        ModelSourceCacheKey(
            source_id=plan.absent_source_id,
            params_digest=ModelSourceCacheKey.digest({"workflow": plan.workflow}),
            revision=plan.head,
        )
        if plan.head is not None
        else None
    )
    if absent_key is not None:
        remembered = cache.lookup(
            absent_key, plan.decode, usable=plan.is_missing, absent=True
        )
        if remembered is not None:
            return plan.with_read_at(remembered, now)
    slot = schedule_slot(plan.crons, now)
    if slot is None:
        fresh = cache.fetch_source(plan.fetch)
        if absent_key is not None and plan.is_missing(fresh):
            cache.store(absent_key, fresh, plan.encode)
        return fresh
    digest = ModelSourceCacheKey.digest(plan.params)
    slot_key = ModelSourceCacheKey(
        source_id=plan.source_id,
        params_digest=digest,
        revision=slot.strftime(SLOT_STAMP),
    )
    latest_key = ModelSourceCacheKey(
        source_id=plan.source_id, params_digest=digest, revision=LATEST_REVISION
    )
    current = cache.lookup(
        slot_key,
        plan.decode,
        usable=lambda value: plan.is_current(value, slot),
        count_miss=False,
    )
    if current is not None:
        # A hit is current by its key (the same slot, the slot's run finished): it carries this cycle's read time.
        return plan.with_read_at(current, now)
    interval = recheck if recheck is not None else pending_recheck_interval()
    # A backend that did not answer the slot lookup is not asked again: the read goes to the source (the cache can only make a step faster).
    kept = (
        cache.lookup(
            latest_key,
            ModelSlotReadEnvelope.model_validate_json,
            usable=lambda envelope: (
                envelope.slot <= slot and now - envelope.checked_at < interval
            ),
            count_hit=False,
            count_miss=False,
        )
        if cache.tally.unavailable == unavailable_before
        else None
    )
    if kept is not None:
        try:
            pending = plan.decode(kept.read_json)
        except (ValueError, ValidationError):
            pending = None
        if pending is not None:
            cache.note_pending_served()
            # The cached read keeps its own read time: the page shows it as pending since the slot, read at that time.
            return plan.with_pending(pending, slot)
    cache.note_miss()
    value = cache.fetch_source(plan.fetch)
    reachable = cache.tally.unavailable == unavailable_before
    if plan.is_missing(value):
        if absent_key is not None:
            cache.store(absent_key, value, plan.encode)
        return value
    if not plan.is_readable(value):
        return value
    finished = plan.is_current(value, slot)
    if reachable:
        if finished:
            cache.store(slot_key, value, plan.encode)
        cache.store(
            latest_key,
            ModelSlotReadEnvelope(
                slot=slot, checked_at=now, read_json=plan.encode(value)
            ),
            lambda envelope: envelope.model_dump_json(),
        )
    return value if finished else plan.with_pending(value, slot)
