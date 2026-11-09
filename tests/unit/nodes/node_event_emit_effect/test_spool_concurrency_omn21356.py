# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two emitters on one host share one spool directory (OMN-21356).

The default spool directory is shared by every emitter process on the host, and
each emit also drains the whole backlog. So a record this process appended can
be published, acked or quarantined by a peer's drain between this process
listing it and acting on it. Before the fix that race ended the emit with an
uncaught ``FileNotFoundError`` (49 of 100 emitter runs on 2026-10-24), and the
caller read a crash where the record was in fact safely in quarantine.

Each test makes the peer act at the exact point the race opens, rather than
hoping a thread interleaving lands there, and ``test_quarantine_concurrent_threads``
adds a real thread race as a second reading.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiokafka.errors import TopicAuthorizationFailedError

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    ModelQuarantineReason,
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

pytestmark = pytest.mark.unit

_TOPIC = "onex.evt.omniclaude.session-started.v1"


def _record(event_id: str) -> SpoolRecord:
    return SpoolRecord(
        event_id=event_id,
        event_type="session.started",
        topic=_TOPIC,
        tier=EnumDurabilityTier.TELEMETRY,
        payload={"n": event_id},
        partition_key=None,
        correlation_id=None,
        queued_at=datetime.now(UTC),
    )


def _reason(record: SpoolRecord, *, detail: str) -> ModelQuarantineReason:
    return ModelQuarantineReason(
        quarantined_at=datetime.now(UTC),
        reason_code="topic_authorization_denied",
        detail=detail,
        event_id=record.event_id,
        event_type=record.event_type,
        topic=record.topic,
        queued_at=record.queued_at,
    )


def _spool_one(spool: SpoolOutbox, event_id: str) -> tuple[SpoolRecord, Path]:
    record = _record(event_id)
    outcome = spool.append(record)
    assert outcome.spool_file is not None
    return record, outcome.spool_file.path


def _quarantined_names(spool: SpoolOutbox) -> list[str]:
    return sorted(p.name for p in spool.quarantine_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# AC1: quarantine of a record a peer already moved into quarantine
# ---------------------------------------------------------------------------


def test_quarantine_concurrent_peer_moved_it_between_list_and_quarantine(
    tmp_path: Path,
) -> None:
    spool_dir = tmp_path / "spool"
    mine = SpoolOutbox(spool_dir)
    peer = SpoolOutbox(spool_dir)  # a second emitter process on the same host
    record, path = _spool_one(mine, "e1")

    listed = mine.list_pending()  # this process has read the queue ...
    assert [f.path for f in listed] == [path]
    # ... and the peer quarantines the same record before this one acts on it.
    peer.quarantine(path, _reason(record, detail="peer"))
    assert not path.exists()

    destination = mine.quarantine(listed[0].path, _reason(record, detail="mine"))

    assert destination == mine.quarantine_dir / path.name
    assert destination.is_file(), "the record must stay in quarantine"
    reason_file = mine.quarantine_dir / f"{path.stem}.reason.json"
    assert reason_file.is_file(), "its reason file must stay beside it"
    assert json.loads(reason_file.read_text())["event_id"] == "e1"
    assert json.loads(destination.read_text())["event_id"] == "e1"
    assert mine.pending_count() == 0
    assert mine.quarantined_count() == 1


def test_quarantine_concurrent_threads(tmp_path: Path) -> None:
    """Real thread race on one record: nobody raises, one record, one reason."""
    spool_dir = tmp_path / "spool"
    seed = SpoolOutbox(spool_dir)
    record, path = _spool_one(seed, "e1")
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def _worker(n: int) -> None:
        outbox = SpoolOutbox(spool_dir)
        try:
            barrier.wait(timeout=10)
            outbox.quarantine(path, _reason(record, detail=f"worker-{n}"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert _quarantined_names(seed) == [path.name, f"{path.stem}.reason.json"]
    assert seed.pending_count() == 0
    assert list(seed.quarantine_dir.glob("*.tmp-*")) == [], "no temp reason left"


# ---------------------------------------------------------------------------
# AC2: an emit whose own record a concurrent backlog drain quarantined
# ---------------------------------------------------------------------------


class _PeerQuarantinesThenRefuses:
    """The broker refuses on ACLs, and a peer's drain got to the record first.

    ``publish`` is where this emitter's own appended record is in flight. The
    peer (a second SpoolOutbox on the same directory, standing for another
    process's backlog drain) quarantines every pending record, then the broker's
    refusal reaches this process, which goes on to quarantine the same record.
    """

    def __init__(self, spool_dir: Path) -> None:
        self._peer = SpoolOutbox(spool_dir)
        self.attempts: list[str] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.attempts.append(topic)
        for pending in self._peer.list_pending():
            self._peer.quarantine(
                pending.path, _reason(pending.record, detail="peer drain")
            )
        raise RuntimeError("publish refused") from TopicAuthorizationFailedError(topic)


def test_concurrent_drain_reports_quarantined_not_published(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool = SpoolOutbox(spool_dir)
    adapter = _PeerQuarantinesThenRefuses(spool_dir)
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "s"})
    )  # must return: an exception here is the exit-1 FileNotFoundError

    assert adapter.attempts, "the record must have been in flight"
    assert result.published is False
    assert result.topics_published == []
    assert result.quarantined_count == 1
    assert result.spool_only is False
    assert spool.pending_count() == 0
    names = _quarantined_names(spool)
    assert len(names) == 2
    assert any(n.endswith(".reason.json") for n in names)
    assert spool.quarantined_count() == 1


def test_concurrent_drain_reports_quarantined_positive_control(
    tmp_path: Path,
) -> None:
    """With no peer the same refusal still quarantines exactly one record."""
    spool_dir = tmp_path / "spool"
    spool = SpoolOutbox(spool_dir)

    class _Refuses:
        def publish(self, topic: str, payload: JsonType, **_kw: object) -> None:
            raise RuntimeError("publish refused") from TopicAuthorizationFailedError(
                topic
            )

    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=_Refuses())
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "s"})
    )

    assert (result.published, result.quarantined_count) == (False, 1)
    assert spool.quarantined_count() == 1


# ---------------------------------------------------------------------------
# AC3: ack of a record a peer already acked or moved
# ---------------------------------------------------------------------------


def test_ack_concurrent_peer_already_acked(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    mine = SpoolOutbox(spool_dir)
    peer = SpoolOutbox(spool_dir)
    _, path = _spool_one(mine, "e1")

    peer.ack(path)  # the peer published and acked it first
    mine.ack(path)  # must raise nothing

    assert mine.pending_count() == 0
    assert mine.quarantined_count() == 0


def test_ack_concurrent_peer_moved_it_to_quarantine(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    mine = SpoolOutbox(spool_dir)
    peer = SpoolOutbox(spool_dir)
    record, path = _spool_one(mine, "e1")

    peer.quarantine(path, _reason(record, detail="peer"))
    mine.ack(path)  # must raise nothing, and must not touch the quarantined copy

    assert mine.quarantined_count() == 1
    assert (mine.quarantine_dir / path.name).is_file()


def test_ack_concurrent_threads(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    seed = SpoolOutbox(spool_dir)
    _, path = _spool_one(seed, "e1")
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
            SpoolOutbox(spool_dir).ack(path)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert seed.pending_count() == 0


def test_ack_concurrent_peer_published_and_acked_during_emit(tmp_path: Path) -> None:
    """Handler level: a peer drain published and acked this emit's own record."""
    spool_dir = tmp_path / "spool"
    spool = SpoolOutbox(spool_dir)

    class _PeerAcksThenPublishes:
        def __init__(self) -> None:
            self._peer = SpoolOutbox(spool_dir)
            self.published: list[str] = []

        def publish(self, topic: str, payload: JsonType, **_kw: object) -> None:
            for pending in self._peer.list_pending():
                self._peer.ack(pending.path)
            self.published.append(topic)

    adapter = _PeerAcksThenPublishes()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "s"})
    )

    assert result.published is True
    assert result.quarantined_count == 0
    assert spool.pending_count() == 0
    assert adapter.published
