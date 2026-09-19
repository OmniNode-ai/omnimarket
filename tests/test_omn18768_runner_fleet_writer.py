# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18768 AC2/AC3 — the runner-fleet writer's DB and snapshot behaviour.

The exposure tests prove the contract SAYS `bus_backed: true`. These prove the
writer makes that true: every row it writes is published as a keyed delta, and
every runner it deletes publishes a real tombstone. An exposure declared
bus-backed whose writer publishes nothing serves a confident empty cache, which
is strictly worse than the honest `not_yet_bus_backed` refusal it replaced.

The writer is dispatched IN-PROCESS by the runtime, once per message, through a
synchronous ``handle()`` that opens its own event loop. Two consecutive
messages are driven here because one cannot expose a loop-bound resource cached
across calls — the defect that left ``consumer_flow_windows`` at 0 rows with 34
``Event loop is closed`` on the dev lane (OMN-16874).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_fleet_liveness_writer import (
    FleetLivenessProjectionWriter,
)

_T0 = datetime(2026, 9, 18, 23, 30, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.omnibase-infra.runner-fleet.v1"
_HOST = "omni-201"

pytestmark = pytest.mark.unit


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for ``AsyncpgAdapter`` and enforces its loop affinity."""

    def __init__(
        self, known: list[str] | None = None, *, refuse_stale: bool = False
    ) -> None:
        self._pool: _LoopBoundPool | None = None
        self.known = known or []
        self.refuse_stale = refuse_stale
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.calls.append((query, params))
        if "SELECT runner_name" in query:
            return [{"runner_name": name} for name in self.known]
        if "DELETE FROM" in query:
            return []
        if "RETURNING" in query:
            if self.refuse_stale:
                # The ON CONFLICT ... WHERE guard refused the write: the stored
                # observation is not older than this one.
                return []
            return [
                {
                    "projection_cursor": 1,
                    "first_seen_at": _T0,
                    "updated_at": _T0,
                }
            ]
        return []


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def __call__(self, exposure: Any, **kwargs: Any) -> bool:
        self.published.append(dict(kwargs))
        return True


def _observation(
    runners: list[dict[str, Any]], *, observed_at: datetime = _T0
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_type": "runner-fleet-observation",
        "host": _HOST,
        "observed_at": observed_at.isoformat(),
        "runners": runners,
        "_topic": _IN_TOPIC,
    }


def _runner(
    name: str, *, status: str = "online", observed_at: datetime = _T0
) -> dict[str, Any]:
    return {
        "runner_name": name,
        "runner_id": 1,
        "label_class": "omnibase-ci",
        "labels": ["self-hosted", "omnibase-ci"],
        "host": _HOST,
        "status": status,
        "current_job_id": None,
        "observed_at": observed_at.isoformat(),
    }


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> FleetLivenessProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = FleetLivenessProjectionWriter()
    instance._db = _RecordingAdapter()  # type: ignore[assignment]
    return instance


def test_the_writer_declares_the_in_process_dispatch_capability() -> None:
    """The runtime routes on a declared capability, never on a class name.

    This class is named ``...Writer`` because the OMN-14350 type-word ratchet
    hard-fails ``Runner``; without the declaration it would silently land on
    the other dispatch branch, where the runtime pre-connects its pool on a
    throwaway loop and every message afterwards dies.
    """
    assert FleetLivenessProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_writer_publishes_a_snapshot_delta_for_every_written_row(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 — the exposure is genuinely bus-backed, not merely declared so."""
    publisher = _RecordingPublisher()
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)

    result = writer.handle(
        _observation([_runner("omninode-runner-1"), _runner("omninode-runner-2")])
    )

    assert result["rows_upserted"] == 2
    assert len(publisher.published) == 2
    assert {call["op"] for call in publisher.published} == {"upsert"}
    names = {call["row"]["runner_name"] for call in publisher.published}
    assert names == {"omninode-runner-1", "omninode-runner-2"}
    # The delta carries the database-assigned columns, not only the derived
    # ones: a reader paginating on projection_cursor needs it in the payload.
    assert publisher.published[0]["row"]["projection_cursor"] == 1
    # And the source coordinates the cache keys its staleness comparison on.
    assert publisher.published[0]["source_topic"] == _IN_TOPIC


def test_a_disappeared_runner_publishes_a_tombstone(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 — the delete publishes a real tombstone so the cache reclaims the key.

    A deregistered runner whose cached row survived would keep reporting
    `online` to every panel that reads the exposure.
    """
    writer._db = _RecordingAdapter(known=["omninode-runner-1", "omninode-runner-2"])  # type: ignore[assignment]
    publisher = _RecordingPublisher()
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)

    result = writer.handle(_observation([_runner("omninode-runner-1")]))

    assert result["rows_tombstoned"] == 1
    assert result["tombstoned_runner_names"] == ["omninode-runner-2"]
    deletes = [call for call in publisher.published if call["op"] == "delete"]
    assert len(deletes) == 1
    # A tombstone carries no row and derives its key from `key` (OMN-16150).
    assert deletes[0]["row"] is None
    assert deletes[0]["key"] == {"runner_name": "omninode-runner-2"}


def test_the_tombstone_is_scoped_to_supersession_and_this_observer(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOTH predicates, because each one alone is a real defect.

    The adversarial reviewer blocked this node once on each horn, and both
    findings were correct about the shape they saw.

    * Host alone. The primary key is ``runner_name`` ALONE (it must be: the
      exposure's snapshot key is ``runner_name``, one row per runner), so an
      upsert rewrites ``observing_host`` and row ownership moves on every
      write. A deregistered runner is then tombstoned only if its momentary
      owner happens to run next, and otherwise LINGERS REPORTING ONLINE.
    * Supersession alone. An observer reporting a NARROWER slice of the pool
      sees a peer's rows as superseded and deletes runners it never observed.

    The conjunction has neither: a runner absent from an observation is not
    upserted, so its row's owner is stably the last observer that SAW it, and
    that observer removes it next cycle -- and a peer's rows are never this
    observation's to delete.
    """
    adapter = _RecordingAdapter(known=["omninode-runner-1", "omninode-runner-9"])
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())

    writer.handle(_observation([_runner("omninode-runner-1")]))

    selects = [c for c in adapter.calls if "SELECT runner_name" in c[0]]
    deletes = [c for c in adapter.calls if "DELETE FROM" in c[0]]
    assert selects, "the known-runner lookup was never issued"
    assert selects[0][1] == (_T0, _HOST), (
        "the lookup must bound on BOTH supersession and this observer: "
        f"{selects[0][1]!r}"
    )
    assert deletes, "the disappeared runner was never deleted"
    assert deletes[0][1] == ("omninode-runner-9", _T0, _HOST)
    # BOTH predicates, in both statements. Either one alone is a real defect
    # and the reviewer blocked this node once on each.
    assert "observed_at <" in selects[0][0]
    assert "observing_host" in selects[0][0]
    assert "observed_at <" in deletes[0][0]
    assert "observing_host" in deletes[0][0]


def test_neither_predicate_may_be_dropped_from_either_statement(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned on the real statements, so removing one is a red test.

    This is the regression guard for an oscillation that actually happened:
    the node shipped host-only, was blocked for the lingering-online hazard,
    was changed to supersession-only, and was blocked for cross-observer
    deletion. Only the conjunction answers both, so both predicates are
    asserted present rather than either one being asserted absent.
    """
    adapter = _RecordingAdapter(known=["omninode-runner-1"])
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())

    writer.handle(_observation([_runner("omninode-runner-1")]))

    selects = [c for c in adapter.calls if "SELECT runner_name" in c[0]]
    assert "observed_at <" in selects[0][0], (
        "without the supersession bound the lookup hands the reducer rows a "
        "peer observer had just written, and they are tombstoned"
    )
    assert "observing_host" in selects[0][0], (
        "without the observer bound a narrower observer deletes runners it "
        "never observed"
    )


def test_a_stale_observation_does_not_overwrite_a_newer_row(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is enforced in SQL, not read-then-write.

    And a refused write is NOT republished: pushing an older row at the cache
    would undo a newer one that the database itself declined to overwrite.
    """
    writer._db = _RecordingAdapter(refuse_stale=True)  # type: ignore[assignment]
    publisher = _RecordingPublisher()
    monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)

    result = writer.handle(_observation([_runner("omninode-runner-1")]))

    assert result["rows_upserted"] == 0
    assert publisher.published == []


def test_the_upsert_guard_is_in_the_sql_not_in_python(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-compare-write would race under concurrent consumers."""
    adapter = _RecordingAdapter()
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())

    writer.handle(_observation([_runner("omninode-runner-1")]))

    upserts = [c for c in adapter.calls if "ON CONFLICT" in c[0]]
    assert upserts, "no upsert was issued"

    sql = upserts[0][0]
    assert "observed_at < EXCLUDED.observed_at" in sql
    assert "RETURNING" in sql


def test_two_consecutive_messages_both_write(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop-scoped pool defect is invisible to a single-message test.

    ``handle()`` opens a loop per message with ``asyncio.run``, which closes it
    on return; anything cached across calls belongs to a dead loop.
    """
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())
    first = writer.handle(_observation([_runner("omninode-runner-1")]))
    second = writer.handle(
        _observation(
            [_runner("omninode-runner-1")], observed_at=_T0 + timedelta(minutes=3)
        )
    )
    assert first["rows_upserted"] == 1
    assert second["rows_upserted"] == 1


def test_the_applied_payload_carries_the_fleet_facts_not_a_bare_ack(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truthy ack over an observation that wrote nothing is indistinguishable
    from one that wrote the whole fleet."""
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())
    result = writer.handle(
        _observation([_runner("omninode-runner-1", status="offline")])
    )
    assert set(result) == {
        "rows_upserted",
        "rows_tombstoned",
        "runner_rows",
        "tombstoned_runner_names",
    }
    assert result["runner_rows"][0]["status"] == "offline"


def test_the_labels_are_written_as_json(
    writer: FleetLivenessProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column is JSONB; a Python list handed to asyncpg is not."""
    adapter = _RecordingAdapter()
    writer._db = adapter  # type: ignore[assignment]
    monkeypatch.setattr(writer, "publish_snapshot_delta", _RecordingPublisher())
    writer.handle(_observation([_runner("omninode-runner-1")]))
    upsert = next(c for c in adapter.calls if "ON CONFLICT" in c[0])
    assert json.loads(upsert[1][3]) == ["self-hosted", "omnibase-ci"]
