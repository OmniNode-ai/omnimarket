# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18159 Phase 1b(i): the in-process delegation path attests and republishes.

WHAT THIS CLOSES

``node_projection_delegation``'s contract declares the per-row exposure
``onex.snapshot.projection.delegation.decisions.v1`` ``bus_backed``, and until
now ``DelegationProjectionRunner`` was its only producer. The contract itself
records the gap in as many words: the sync handler "DOES NOT PUBLISH ... it
writes through the shared ``DatabaseAdapter.upsert`` protocol, which returns a
bool -- it never sees the stored row, so it cannot republish the two columns
Postgres stamps", and it names the fix as "its own change with three adapter
implementations in scope".

That change landed. ``ProtocolProjectionAttestedWrite.upsert_returning`` now
carries SQL-expression columns and ``RETURNING``, so this module closes the
half the contract was waiting on: the in-process path stamps the attestation
and republishes the row POSTGRES stored, on every delegation-table write path,
not just one of them.

WHY ALL THREE WRITE PATHS GO THROUGH ONE METHOD

``project``, ``project_delegate_skill_terminal`` and
``project_quality_gate_result`` all upsert ``delegation_events``. A row that
was durable from one path and invisible to a reader from another would be
worse than either state alone, and worse than the current honest "no sync
publisher at all", because it would look like the exposure works. The runner
already funnels its two full-row paths through a single site for exactly this
reason; this mirrors it and adds the third.

WHY A MISSING CAPABILITY IS A REFUSAL, NOT A FALLBACK

The runtime kernel's own adapter does not implement the attested write yet
(OMN-18159 AC5). If this handler quietly fell back to the plain ``upsert``
when the capability is absent, the row would still be written -- and
``writer_identity`` would be NULL on every update arm, because a column
DEFAULT is consulted only on INSERT. A NULL there reads to the green bar's
leg 4 exactly like "nobody wrote this", which is indistinguishable from "an
unscoped principal wrote this". The refusal names the adapter instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import (
    InmemoryDatabaseAdapter,
    ProtocolProjectionAttestedWrite,
)
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage

pytestmark = pytest.mark.unit

DECISIONS_TOPIC = "onex.snapshot.projection.delegation.decisions.v1"


class RecordingPublisher:
    """Captures every published delta instead of reaching a broker."""

    def __init__(self) -> None:
        self.messages: list[ModelSnapshotDeltaMessage] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.messages.append(message)
        return True


class UpsertOnlyAdapter:
    """A store with the base protocol and NOT the attested-write capability.

    This is the shape of the runtime kernel's own adapter today, and of the
    roughly twenty three-argument doubles across this repo. Keeping one here
    is what makes the refusal test real rather than hypothetical.
    """

    def __init__(self) -> None:
        self.inner = InmemoryDatabaseAdapter()

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        return self.inner.upsert(table, conflict_key, row)

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return self.inner.query(
            table, filters, order_by=order_by, descending=descending, limit=limit
        )


CORRELATION_ID = "2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f80"


def _handler(
    publisher: RecordingPublisher | None = None,
) -> HandlerProjectionDelegation:
    return HandlerProjectionDelegation(publisher=publisher)


def _task_delegated() -> ModelTaskDelegatedEvent:
    return ModelTaskDelegatedEvent(
        correlation_id=CORRELATION_ID,
        session_id="s1",
        task_type="code_review",
        delegated_to="local",
        model_name="qwen",
        delegated_by="test",
        quality_gate_passed=True,
        timestamp="2026-09-11T00:00:00+00:00",
    )


def _delegate_skill_terminal() -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {
            "status": "completed",
            "correlation_id": CORRELATION_ID,
            "task_type": "code_generation",
            "provider": "local-qwen",
            "model_name": "qwen",
            "response": "evidence proof",
        }
    )


def _quality_gate_result() -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=UUID(CORRELATION_ID),
        passed=True,
        quality_score=0.91,
        actual_score=0.91,
    )


def _drive(handler: HandlerProjectionDelegation, path: str, db: Any) -> None:
    """Drive one of the three real delegation-table write paths.

    Deliberately the public methods rather than a test-only hook on the
    handler: a seam that exists only for tests proves the seam, not the path
    a message actually takes.
    """
    if path == "terminal":
        handler.project(_task_delegated(), db)
    elif path == "delegate_skill_terminal":
        handler.project_delegate_skill_terminal(_delegate_skill_terminal(), db)
    elif path == "quality_gate_result":
        handler.project_quality_gate_result(
            _quality_gate_result(),
            db,
            tenant_identity=None,
            event_timestamp=datetime(2026, 9, 11, tzinfo=UTC),
        )
    else:  # pragma: no cover - guards a typo in a parametrize list
        raise AssertionError(f"unknown write path {path!r}")


class TestTheCapabilityIsRequiredNotOptional:
    def test_the_inmemory_adapter_has_the_capability(self) -> None:
        assert isinstance(InmemoryDatabaseAdapter(), ProtocolProjectionAttestedWrite)

    def test_an_upsert_only_adapter_does_not(self) -> None:
        assert not isinstance(UpsertOnlyAdapter(), ProtocolProjectionAttestedWrite)

    def test_a_store_without_the_capability_is_refused_by_name(self) -> None:
        """Refused rather than silently written with a NULL attestation.

        Writing anyway is the tempting branch and it is the wrong one: the
        row would persist, the page would look populated, and the column the
        whole ticket exists to produce would be NULL on every update arm.
        """
        handler = _handler(RecordingPublisher())
        with pytest.raises(TypeError, match="UpsertOnlyAdapter"):
            _drive(handler, "terminal", UpsertOnlyAdapter())


class TestEveryDelegationWritePathAttestsAndRepublishes:
    """One row visible from one path and not another is the failure to avoid."""

    @pytest.mark.parametrize(
        "path",
        ["terminal", "delegate_skill_terminal", "quality_gate_result"],
    )
    def test_the_path_publishes_exactly_one_decisions_delta(self, path: str) -> None:
        publisher = RecordingPublisher()
        handler = _handler(publisher)
        db = InmemoryDatabaseAdapter()
        _drive(handler, path, db)
        topics = [m.topic for m in publisher.messages]
        assert topics.count(DECISIONS_TOPIC) == 1, topics

    @pytest.mark.parametrize(
        "path",
        ["terminal", "delegate_skill_terminal", "quality_gate_result"],
    )
    def test_the_path_stamps_the_attestation_columns(self, path: str) -> None:
        db = InmemoryDatabaseAdapter()
        _drive(_handler(RecordingPublisher()), path, db)
        stored = db.query("delegation_events")[0]
        # The double cannot evaluate CURRENT_USER, so it records a sentinel.
        # Asserting the sentinel rather than a plausible principal is what
        # keeps this from passing against a path that bound a literal.
        assert str(stored["writer_identity"]).startswith("<sql:")
        # NOW() is different on purpose: the double gives a real clock,
        # because the republish derives its ordering token from written_at
        # and a fixed value would drop every write after the first as stale.
        assert datetime.fromisoformat(str(stored["written_at"])) <= datetime.now(tz=UTC)


class TestThePublishedRowIsTheStoredRow:
    def test_it_publishes_what_the_database_returned_not_the_handlers_dict(
        self,
    ) -> None:
        """The two attested columns cannot come from the writer's own dict.

        They are stamped by the database, so a republish assembled from the
        row the handler built would serve an attestation nothing attested to.
        """
        publisher = RecordingPublisher()
        db = InmemoryDatabaseAdapter()
        _drive(_handler(publisher), "terminal", db)
        delta = next(m for m in publisher.messages if m.topic == DECISIONS_TOPIC)
        payload = json.loads(delta.value.decode())
        assert str(payload["row"]["writer_identity"]).startswith("<sql:")

    def test_the_delta_is_keyed_on_the_tables_own_conflict_key(self) -> None:
        """A snapshot key disagreeing with the table's uniqueness is a bug.

        It would either collapse two rows onto one cache entry or leave a
        superseded row that compaction can never reclaim.
        """
        publisher = RecordingPublisher()
        _drive(_handler(publisher), "terminal", InmemoryDatabaseAdapter())
        delta = next(m for m in publisher.messages if m.topic == DECISIONS_TOPIC)
        assert CORRELATION_ID in delta.key.decode()

    def test_a_write_that_stored_nothing_publishes_nothing(self) -> None:
        """No stored row means there is nothing to describe.

        Inventing a delta here would be the confident-empty failure inverted:
        a reader would be served a row the database does not hold.
        """
        publisher = RecordingPublisher()
        handler = _handler(publisher)

        class NoRowsAdapter(InmemoryDatabaseAdapter):
            def upsert_returning(self, *a: Any, **k: Any) -> list[dict[str, object]]:
                super().upsert_returning(*a, **k)
                return []

        _drive(handler, "terminal", NoRowsAdapter())
        assert [m for m in publisher.messages if m.topic == DECISIONS_TOPIC] == []


class TestTheRunnerRemainsTheOtherProducer:
    """Phase 1b adds a producer; it does not remove one.

    The plan's invariant 6 is that no exposure loses its publisher. Until the
    runner's routing entry is dropped in Phase 2, both paths can publish this
    exposure and the compaction key makes that idempotent -- the same
    correlation_id resolves to the same cache entry whichever writer produced
    it. Asserting the key agreement here is what makes the overlap safe rather
    than merely believed to be.
    """

    def test_both_producers_key_the_exposure_the_same_way(self) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers import (
            handler_delegation,
        )

        publisher = RecordingPublisher()
        _drive(_handler(publisher), "terminal", InmemoryDatabaseAdapter())
        delta = next(m for m in publisher.messages if m.topic == DECISIONS_TOPIC)
        assert handler_delegation._DELEGATION_ROW_KEY == "correlation_id"
        assert CORRELATION_ID in delta.key.decode()


class TestTheOrderingTokenSurvivesARewrite:
    """The hazard that fixed coordinates would have hidden.

    ``SnapshotCache`` drops a delta whose ``source_offset`` is ``<=`` the
    cached one for the same topic and partition. This exposure's key is
    ``correlation_id``, which is MUTABLE: a terminal writes the row and a
    quality-gate verdict rewrites it. With a fixed offset the second delta is
    dropped, the page keeps serving a row that is real but stale, and every
    write still reports success -- a failure nothing downstream would surface.
    """

    def test_a_second_write_to_the_same_key_carries_a_higher_offset(self) -> None:
        publisher = RecordingPublisher()
        handler = _handler(publisher)
        db = InmemoryDatabaseAdapter()
        _drive(handler, "terminal", db)
        _drive(handler, "quality_gate_result", db)
        deltas = [m for m in publisher.messages if m.topic == DECISIONS_TOPIC]
        assert len(deltas) == 2
        offsets = [json.loads(m.value.decode())["source_offset"] for m in deltas]
        assert offsets[1] > offsets[0], offsets

    def test_both_writes_key_the_same_cache_entry(self) -> None:
        """The offsets only matter because the two deltas collide on one key."""
        publisher = RecordingPublisher()
        handler = _handler(publisher)
        db = InmemoryDatabaseAdapter()
        _drive(handler, "terminal", db)
        _drive(handler, "quality_gate_result", db)
        deltas = [m for m in publisher.messages if m.topic == DECISIONS_TOPIC]
        assert deltas[0].key == deltas[1].key

    def test_an_unusable_write_stamp_is_refused_rather_than_zeroed(self) -> None:
        """A zero fallback would reintroduce the bug this token prevents.

        It would publish a delta the cache silently drops for every row that
        already has one, so the page would go stale while each write reported
        success.
        """
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            _write_ordering_token,
        )

        with pytest.raises(RuntimeError, match="ordering token"):
            _write_ordering_token("not-a-timestamp")
        with pytest.raises(RuntimeError, match="not a timestamp"):
            _write_ordering_token(None)


# --------------------------------------------------------------------------
# Real Postgres -- the only place the column types and the stamp can be proven
# --------------------------------------------------------------------------
#
# The projection write-path gate requires this, and the requirement is right:
# a mock-DB-only test cannot catch a str-vs-datetime column-type mismatch, and
# this change makes `written_at` load-bearing twice -- once as an attestation
# column the database stamps, and once as the ordering token the republish
# derives from it. Only a real connection proves that what comes back through
# RETURNING is a timestamp the token function can read.

# Reusing that module's migrated-schema fixture rather than rebuilding the
# node's whole migration chain here: one schema builder for this node means a
# migration added tomorrow reaches both proofs. The F811 suppressions below
# are the standard pytest fixture-reuse collision -- the test parameter must
# share the fixture's name for pytest to resolve it, which ruff reads as a
# redefinition of the import.
from tests.test_omn16804_registry_resolved_write_tenant_real_postgres import (  # noqa: E402
    real_pg_adapter,  # noqa: F401
)


@pytest.mark.integration
class TestAgainstRealPostgres:
    def test_the_database_stamps_the_writer_and_the_row_comes_back(
        self,
        real_pg_adapter: Any,  # noqa: F811
    ) -> None:
        """Postgres evaluates CURRENT_USER; this process never supplies it."""
        publisher = RecordingPublisher()
        _drive(_handler(publisher), "terminal", real_pg_adapter)

        rows = real_pg_adapter.query(
            "delegation_events", {"correlation_id": CORRELATION_ID}
        )
        assert len(rows) == 1
        # Not the literal string, not NULL, and not anything this process said.
        assert rows[0]["writer_identity"] not in (None, "", "CURRENT_USER")
        assert isinstance(rows[0]["written_at"], datetime)

    def test_written_at_comes_back_as_a_timestamp_the_token_can_read(
        self,
        real_pg_adapter: Any,  # noqa: F811
    ) -> None:
        """The str-vs-datetime class the write-path gate exists to catch.

        The ordering token parses ``written_at`` out of the RETURNING row. If
        the real driver handed back something the parser refuses, every
        republish would raise at runtime while every double-backed test
        stayed green.
        """
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            _write_ordering_token,
        )

        publisher = RecordingPublisher()
        _drive(_handler(publisher), "terminal", real_pg_adapter)
        delta = next(m for m in publisher.messages if m.topic == DECISIONS_TOPIC)
        token = json.loads(delta.value.decode())["source_offset"]
        assert token > 0
        stored = real_pg_adapter.query(
            "delegation_events", {"correlation_id": CORRELATION_ID}
        )[0]
        assert token == _write_ordering_token(stored["written_at"])

    def test_a_rewrite_restamps_the_row_and_advances_the_token(
        self,
        real_pg_adapter: Any,  # noqa: F811
    ) -> None:
        """The DO UPDATE arm restates the expressions, against a real database.

        This is the property a column DEFAULT cannot give and no double can
        prove: a second write to the same correlation_id must move
        ``written_at`` forward, or the exposure freezes on the first write.
        """
        publisher = RecordingPublisher()
        handler = _handler(publisher)
        _drive(handler, "terminal", real_pg_adapter)
        first = real_pg_adapter.query(
            "delegation_events", {"correlation_id": CORRELATION_ID}
        )[0]["written_at"]
        _drive(handler, "quality_gate_result", real_pg_adapter)
        second = real_pg_adapter.query(
            "delegation_events", {"correlation_id": CORRELATION_ID}
        )[0]["written_at"]
        assert second > first

        deltas = [m for m in publisher.messages if m.topic == DECISIONS_TOPIC]
        offsets = [json.loads(m.value.decode())["source_offset"] for m in deltas]
        assert offsets == sorted(offsets)
        assert offsets[-1] > offsets[0]
