# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15904 -- a restart resumes from committed offsets instead of replaying.

WHAT WAS BROKEN. ``SnapshotCache`` minted a fresh ``uuid4``-suffixed
``group_id`` per process with ``enable_auto_commit=False``. A group nobody has
ever committed to has no committed offset, so with
``auto_offset_reset="earliest"`` EVERY process start replayed the whole retained
topic from offset 0. OMN-15876 made that replay converge (bounded batches); it
did not stop it happening.

Measured on onex-dev 2026-09-26, same workload, same day:

    deploy 36243485542 (12:55Z)   rollout 13:22:12 -> 13:37:15 =   903s   PASS
    deploy 36267711095 (19:55Z)   rollout 20:15:22 -> 20:45:23 = >1800s   FAIL

1800s is ``progressDeadlineSeconds`` on the Deployment, so the container was
killed mid-replay, the post-deploy rollout wait failed, and the deploy failed
with it. Thirteen consecutive staging deploys failed that way between
2026-09-26T19:03Z and 2026-09-27T14:44Z, and three of M2's five legs score that
failed deploy -- so C1, C2 and C3 all sat behind this one mechanism.

WHY THE OBVIOUS FIX WAS NOT SAFE. The ticket's own title asks for a stable
``group_id``, and a stable id ALONE would have been a regression: the per-process
uuid4 was what guaranteed every replica of this full-topic state cache received
every partition. Share one coordinated group between two replicas and Kafka
splits the partitions, so each caches a subset while both report
``bootstrap_complete=True`` -- a silent wrong answer, worse than a slow start.

THE SHAPE THAT IS SAFE. ``start()`` assigns partitions MANUALLY. ``assign()``
never consults the group coordinator, so full coverage holds by construction and
stops depending on id uniqueness -- which frees the id to be stable, which is
what lets a restart resume. The id's only remaining job is to name where offsets
are committed.

These tests pin both halves and the four ways the change could go wrong: a
resume that does not happen, a first-ever start that wrongly skips its replay, a
commit that lands before the rows it promises, and an assignment that silently
covers nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from aiokafka import TopicPartition

from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_TOPIC_A = "onex.snapshot.projection.consumer-flow.v1"
_TOPIC_B = "onex.snapshot.projection.prod-promotion-gate.v1"
_PARTITIONS = 3


def _cfg(topic: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="t",
        columns=("k", "v"),
        bus_backed=True,
        key_columns=("k",),
        key_grain="mutable",
    )


class _Msg:
    """A real upsert delta, same shape as the OMN-18955 suite's.

    A hand-rolled payload is rejected by ``ModelProjectionSnapshotDelta`` and
    applies no row, which makes the commit-ordering assertions below vacuous --
    they would pass over a cache that applied nothing.
    """

    def __init__(self, *, topic: str, partition: int, offset: int) -> None:
        key = f"k{offset % 10}"
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.key = key.encode()
        self.value = (
            ModelProjectionSnapshotDelta(
                topic=topic,
                key=(key,),
                op="upsert",
                row={"k": key, "v": offset},
                observed_at="2026-09-27T04:00:00Z",
                source_event_id=f"evt-{partition}-{offset}",
                source_topic="src",
                source_partition=partition,
                source_offset=offset,
            )
            .model_dump_json()
            .encode()
        )
        self.headers: list[tuple[str, bytes]] = []


class _FakeConsumer:
    """Enough of aiokafka to exercise assign / committed / seek / commit.

    ``committed_offsets`` is the broker-side offset store: what a PREVIOUS
    process of the same group left behind. That is the only channel through which
    a restart can learn where to resume, which is what makes it the subject of
    these tests.
    """

    def __init__(
        self,
        *,
        end: int = 10_000,
        committed: dict[TopicPartition, int] | None = None,
        partitions: int = _PARTITIONS,
        commit_raises: BaseException | None = None,
        topics_without_metadata: frozenset[str] = frozenset(),
        metadata_after_refreshes: int = 0,
    ) -> None:
        #: OMN-15904 cold lane: how many metadata refreshes must happen before
        #: topics resolve. 0 = already present (a warm lane, e.g. onex-dev).
        self._metadata_after_refreshes = metadata_after_refreshes
        self.metadata_refreshes = 0
        self._end = end
        self._partitions = partitions
        self._topics_without_metadata = topics_without_metadata
        self.committed_offsets: dict[TopicPartition, int] = dict(committed or {})
        self.assigned: list[TopicPartition] = []
        self.seeks: list[tuple[TopicPartition, int]] = []
        self.commits: list[dict[TopicPartition, int]] = []
        self.position_by_tp: dict[TopicPartition, int] = {}
        self._commit_raises = commit_raises
        self.subscribed: Any = None

    # -- assignment ------------------------------------------------------
    async def topics(self) -> set[str]:
        """Force a metadata refresh, as the real client does."""
        self.metadata_refreshes += 1
        await asyncio.sleep(0)
        return {_TOPIC_A, _TOPIC_B}

    def partitions_for_topic(self, topic: str) -> set[int] | None:
        if topic in self._topics_without_metadata:
            return None
        if self.metadata_refreshes < self._metadata_after_refreshes:
            # A cold lane: the topic does not exist yet because its producer
            # has not published. Resolves once enough refreshes have happened.
            return None
        return set(range(self._partitions))

    def assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = list(partitions)
        self.position_by_tp = dict.fromkeys(partitions, 0)

    def assignment(self) -> frozenset[TopicPartition]:
        return frozenset(self.assigned)

    def subscribe(self, topics: list[str], listener: Any = None) -> None:
        self.subscribed = (list(topics), listener)

    # -- offsets ---------------------------------------------------------
    async def committed(self, tp: TopicPartition) -> int | None:
        return self.committed_offsets.get(tp)

    async def commit(self, offsets: dict[TopicPartition, int]) -> None:
        if self._commit_raises is not None:
            raise self._commit_raises
        self.commits.append(dict(offsets))
        self.committed_offsets.update(offsets)

    def seek(self, tp: TopicPartition, offset: int) -> None:
        self.seeks.append((tp, offset))
        self.position_by_tp[tp] = offset

    # -- fetching --------------------------------------------------------
    def highwater(self, tp: TopicPartition) -> int | None:
        return self._end

    async def end_offsets(
        self, partitions: list[TopicPartition]
    ) -> dict[TopicPartition, int]:
        return dict.fromkeys(partitions, self._end)

    async def position(self, tp: TopicPartition) -> int:
        return self.position_by_tp.get(tp, 0)

    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[_Msg]]:
        batches: dict[TopicPartition, list[_Msg]] = {}
        for tp, pos in self.position_by_tp.items():
            take = min(self._end - pos, max_records or self._end)
            if take > 0:
                batches[tp] = [
                    _Msg(topic=tp.topic, partition=tp.partition, offset=pos + i)
                    for i in range(take)
                ]
                self.position_by_tp[tp] = pos + take
        await asyncio.sleep(0)
        return batches

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fast_bootstrap_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same accommodation the OMN-18955 suite makes: don't sleep 20s per test."""
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)


async def _consume_until_caught_up(cache: SnapshotCache) -> None:
    """Drive the REAL consume loop until both topics are caught up, then stop.

    The loop never returns on its own -- it is a live consumer -- so the test
    drives it exactly the way the OMN-18955 suite does rather than awaiting it.
    """
    cache._running = True
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(10):
            while not (
                cache.is_bootstrapped(_TOPIC_A) and cache.is_bootstrapped(_TOPIC_B)
            ):
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _cache(
    consumer: _FakeConsumer, *, group_id: str = "onex-dev.stable"
) -> SnapshotCache:
    cache = SnapshotCache(
        {_TOPIC_A: _cfg(_TOPIC_A), _TOPIC_B: _cfg(_TOPIC_B)},
        bootstrap_servers="unused:9092",
        group_id=group_id,
    )
    cache._consumer = consumer
    return cache


# ---------------------------------------------------------------------------
# AC1 + the invariant the old mechanism held
# ---------------------------------------------------------------------------


async def test_every_partition_of_every_topic_is_assigned() -> None:
    """The full-topic guarantee, now held by assign() rather than by uuid4.

    This is the case that makes a stable group id safe. If it ever fails, two
    replicas sharing the stable id can be handed disjoint subsets and each will
    report itself bootstrapped over a partial cache.
    """
    consumer = _FakeConsumer()
    cache = _cache(consumer)
    await cache._assign_and_resume()
    assert set(consumer.assigned) == {
        TopicPartition(topic, p)
        for topic in cache.subscription_topics
        for p in range(_PARTITIONS)
    }
    assert consumer.subscribed is None, "assign() and subscribe() are exclusive"


# ---------------------------------------------------------------------------
# AC2 -- the resume regression test the ticket asks for by name
# ---------------------------------------------------------------------------


async def test_a_restart_resumes_at_the_committed_offset_not_zero() -> None:
    """The ticket's AC2, verbatim: commit past N, restart, resume at N.

    'Restart' is a NEW SnapshotCache against a broker that still holds the
    previous process's committed offsets -- which is exactly what a pod restart
    is once the group id is stable.
    """
    n = 7_431
    committed = {TopicPartition(_TOPIC_A, p): n for p in range(_PARTITIONS)}
    consumer = _FakeConsumer(committed=committed)
    cache = _cache(consumer)

    await cache._assign_and_resume()

    for p in range(_PARTITIONS):
        tp = TopicPartition(_TOPIC_A, p)
        assert (tp, n) in consumer.seeks, f"{tp} did not resume at {n}"
        assert consumer.position_by_tp[tp] == n
        assert consumer.position_by_tp[tp] != 0


async def test_a_partition_with_no_committed_offset_still_replays() -> None:
    """A first-ever start must NOT skip its replay.

    ``auto_offset_reset="earliest"`` remains correct for a partition this group
    has never committed: that is a genuine cold start and it does need the whole
    topic. Asserting this keeps the fix from being read as 'never replay'.
    """
    consumer = _FakeConsumer(committed={TopicPartition(_TOPIC_A, 0): 12})
    cache = _cache(consumer)
    await cache._assign_and_resume()

    sought = {tp for tp, _ in consumer.seeks}
    assert TopicPartition(_TOPIC_A, 0) in sought
    assert TopicPartition(_TOPIC_A, 1) not in sought, (
        "a partition with no committed offset must be left at the log start"
    )
    assert TopicPartition(_TOPIC_B, 0) not in sought


async def test_the_resume_seeds_the_in_process_floor_too() -> None:
    """The resumed offset must also become the cache's own applied floor.

    Otherwise the drop-streak accounting starts from zero while the consumer
    starts from N, and every record read looks like a replay of something the
    cache never applied -- OMN-18905's stale latch, reached from the other side.
    """
    n = 500
    consumer = _FakeConsumer(committed={TopicPartition(_TOPIC_A, 1): n})
    cache = _cache(consumer)
    await cache._assign_and_resume()
    state = cache._state[cache.canonical_topic(_TOPIC_A)]
    assert state.applied_position[1] == n
    assert state.next_position[1] == n


# ---------------------------------------------------------------------------
# The commit half -- without it the resume above has nothing to read
# ---------------------------------------------------------------------------


async def test_consuming_a_batch_commits_past_it() -> None:
    consumer = _FakeConsumer(end=50)
    cache = _cache(consumer)
    await cache._assign_and_resume()
    await _consume_until_caught_up(cache)

    assert consumer.commits, "a consumed batch committed nothing"
    merged: dict[TopicPartition, int] = {}
    for c in consumer.commits:
        merged.update(c)
    for tp, offset in merged.items():
        assert offset == 50, f"{tp} committed {offset}, expected past the last record"


async def test_the_commit_promises_only_rows_already_applied() -> None:
    """Commit AFTER apply, never before.

    A committed offset is a promise that everything below it is in ``rows``.
    Committing first would let a restart resume past records it never applied --
    a permanent hole in a cache that reports itself bootstrapped.
    """
    consumer = _FakeConsumer(end=20)
    cache = _cache(consumer)
    await cache._assign_and_resume()
    observed: list[int] = []
    real_commit = consumer.commit

    async def recording_commit(offsets: dict[TopicPartition, int]) -> None:
        # How many rows exist AT THE MOMENT the commit is issued.
        observed.append(len(cache._state[cache.canonical_topic(_TOPIC_A)].rows))
        await real_commit(offsets)

    consumer.commit = recording_commit  # type: ignore[assignment]
    await _consume_until_caught_up(cache)

    assert observed, "commit was never called"
    assert all(count > 0 for count in observed), (
        f"a commit was issued before any row was applied: {observed}"
    )


async def test_a_failing_commit_is_counted_not_fatal() -> None:
    """A broker that refuses a commit must not end consumption.

    The cache is still correct in memory; what is lost is the resume point, so
    the cost lands on the next restart's catch-up. Converting that into a dead
    consumer would trade a slow start for no data at all.
    """
    consumer = _FakeConsumer(end=30, commit_raises=RuntimeError("broker said no"))
    cache = _cache(consumer)
    await cache._assign_and_resume()
    await _consume_until_caught_up(cache)

    assert cache.commit_failures > 0
    assert cache.consume_failure is None, (
        "a commit failure must not be recorded as a consume failure"
    )
    assert cache._state[cache.canonical_topic(_TOPIC_A)].rows, (
        "the cache must still have applied its rows"
    )


async def test_committed_position_is_readable_for_the_live_readback() -> None:
    """AC3 needs an in-process surface, not an inference from an absent replay.

    An uncommitted cache and a committed one look identical until the next
    restart -- which is the property that let this defect sit latent from
    2026-08-11 to 2026-09-26.
    """
    consumer = _FakeConsumer(end=10)
    cache = _cache(consumer)
    await cache._assign_and_resume()
    await _consume_until_caught_up(cache)
    assert cache.committed_position, "committed_position exposes nothing"
    assert all(offset == 10 for offset in cache.committed_position.values())


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.fixture
def _short_metadata_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the refusal cases from waiting the real 300s bound."""
    import omnimarket.projection.snapshot_cache as module

    monkeypatch.setattr(module, "_ASSIGN_METADATA_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "_ASSIGN_METADATA_POLL_SECONDS", 0.01)


@pytest.mark.usefixtures("_short_metadata_bound")
async def test_a_cold_lane_waits_for_metadata_instead_of_refusing() -> None:
    """THE REGRESSION THIS FILE MISSED THE FIRST TIME.

    On a fresh cluster the snapshot topics do not exist yet, so the first
    metadata read returns nothing. The original code raised there, `start()`
    failed, the pod never became Ready -- and because these topics are created
    by their PRODUCERS, a consumer that refuses to start can never be the thing
    that brings them into existence. Measured on candidate delivery 36344681102:
    `omnimarket-projection-api` 0/1 for the boot gate's full 32-minute wait with
    `onex.snapshot.projection.consumer-flow.v1` ABSENT, against 1/1 Ready on the
    pre-change candidate (run 36328593981).

    onex-dev never showed it because the topics already exist there, which is
    exactly why a unit test had to.
    """
    consumer = _FakeConsumer(metadata_after_refreshes=3)
    cache = _cache(consumer)
    await cache._assign_and_resume()

    assert consumer.metadata_refreshes >= 3, (
        "start() gave up before the topics could appear"
    )
    assert set(consumer.assigned) == {
        TopicPartition(topic, p)
        for topic in cache.subscription_topics
        for p in range(_PARTITIONS)
    }


@pytest.mark.usefixtures("_short_metadata_bound")
async def test_metadata_that_never_appears_still_refuses() -> None:
    """The bound is what separates ABSENT-NOW from ABSENT-FOREVER.

    Waiting must not become waiting forever: a topic that is misnamed or
    unprovisioned has to fail the pod, because a consumer hung on metadata is
    indistinguishable at /ready from one replaying slowly.
    """
    consumer = _FakeConsumer(topics_without_metadata=frozenset({_TOPIC_B}))
    cache = _cache(consumer)
    with pytest.raises(RuntimeError, match="no partition metadata"):
        await cache._assign_and_resume()
    assert consumer.assigned == []


async def test_a_warm_lane_does_not_wait_at_all() -> None:
    """onex-dev's shape: topics already exist, so one refresh resolves them.

    A wait that fired on a warm lane would add latency to every ordinary
    restart, which is the thing this whole ticket exists to reduce.
    """
    consumer = _FakeConsumer()
    cache = _cache(consumer)
    await cache._assign_and_resume()
    assert consumer.metadata_refreshes == 1


@pytest.mark.usefixtures("_short_metadata_bound")
async def test_a_topic_with_no_partition_metadata_refuses_to_start() -> None:
    """Assigning nothing would serve zero rows at HTTP 200.

    A topic whose metadata does not resolve must raise, not be skipped: skipping
    it assigns a partial view, consumes nothing for it, and latches
    ``bootstrap_complete`` over an empty cache -- the fail-open shape OMN-18905
    was filed for.
    """
    consumer = _FakeConsumer(topics_without_metadata=frozenset({_TOPIC_B}))
    cache = _cache(consumer)
    with pytest.raises(RuntimeError, match="no partition metadata"):
        await cache._assign_and_resume()
    assert consumer.assigned == [], "nothing may be assigned on the refusal path"


@pytest.mark.usefixtures("_short_metadata_bound")
async def test_the_refusal_names_the_topic_that_could_not_resolve() -> None:
    consumer = _FakeConsumer(topics_without_metadata=frozenset({_TOPIC_B}))
    cache = _cache(consumer)
    with pytest.raises(RuntimeError) as excinfo:
        await cache._assign_and_resume()
    assert _TOPIC_B in str(excinfo.value)
