# SPDX-License-Identifier: MIT
"""node_hook_event_capture: contract, model and idempotency proofs (OMN-16090).

The load-bearing property is the one the ticket's acceptance criterion names:
persisting N events from a batch and then REDELIVERING the same batch must
leave the row count unchanged. That is proven here against a fake DB that
implements the real ``ON CONFLICT (tenant_id, event_sha) DO NOTHING`` semantics
the migration declares — not against a mock that returns whatever the test
wants, which would prove nothing about the conflict target.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.config.settings import get_settings
from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    TABLE,
    HandlerHookEventCapture,
    HookEventCaptureError,
)
from omnimarket.nodes.node_hook_event_capture.models.model_hook_event_capture_request import (
    MAX_EVENTS_PER_BATCH,
    ModelCapturedHookEvent,
    ModelHookEventCaptureRequest,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_event_capture"
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
MIGRATION = (NODE_DIR / "migrations" / "0001_create_hook_events.sql").read_text()
# RLS lives in a SEPARATE migration on purpose: the forward-migration runner
# refuses any new node migration applying FORCE ROW LEVEL SECURITY unless its id
# is in the operator fence, so the create and the RLS posture cannot share a
# file. Splitting them lets the TABLE land on every lane while the RLS posture
# stays under the fence.
MIGRATION_RLS = (
    NODE_DIR / "migrations" / "0002_hook_events_tenant_rls.sql"
).read_text()

PRINCIPAL = "t-" + "0" * 32


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _pin_interim_tenant_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the OMN-14058 house-tenant interim so a lane's real env cannot
    change the resolved tenant out from under an assertion that expects it.

    ``get_settings()`` is ``@lru_cache``d, so mutating the cached instance's
    attributes (the same pattern ``test_house_tenant_default_ratchet.py``
    uses) takes effect immediately without needing to clear the cache.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "onex_tenant_id", "", raising=True)
    monkeypatch.setattr(settings, "enforce_tenant_isolation", False, raising=True)


def _event(seed: str = "e0", **overrides: Any) -> dict[str, Any]:
    """An event shaped from the real spool corpus."""
    event: dict[str, Any] = {
        "event_type": "onex.evt.omniclaude.skill-started.v1",
        "event_sha": _sha(seed),
        "occurred_at": "2026-08-09T15:35:58.797727+00:00",
        "payload_json": json.dumps({"skill_name": "node_dod_verify"}),
    }
    event.update(overrides)
    return event


def _batch(
    events: list[dict[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "local_macos_claude_hooks",
        "batch_sha": _sha("batch"),
        "events": events if events is not None else [_event()],
        "correlation_id": "4a3b2c1d-0000-0000-0000-000000000000",
        "emitted_at": "2026-08-16T18:00:00Z",
        "tenant_id": "omninode",
        "tenant_principal_id": PRINCIPAL,
    }
    payload.update(overrides)
    return payload


class FakeConflictAwareDB:
    """Minimal DB double implementing the migration's conflict target.

    It is deliberately NOT a MagicMock. The property under test is that a
    replay is a no-op *because the unique key is (tenant_id, event_sha)*; a
    mock returning a canned row count would pass no matter what conflict target
    the SQL actually named, which is the failure mode this double exists to
    prevent. Rows are keyed exactly as the UNIQUE constraint keys them.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.tenants_seen: list[str | None] = []
        self.statements: list[str] = []

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        self.statements.append(query)
        self.tenants_seen.append(tenant)
        tenant_id: str = params[0]
        source: str = params[1]
        batch_sha: str = params[2]
        (
            shas,
            types,
            occurred,
            payloads,
            event_ids,
            corr_ids,
            run_ids,
            spooled,
            reasons,
        ) = params[3:12]
        inserted: list[dict[str, Any]] = []
        for i, sha in enumerate(shas):
            key = (tenant_id, sha)
            if key in self.rows:  # ON CONFLICT ... DO NOTHING
                continue
            self.rows[key] = {
                "tenant_id": tenant_id,
                "event_sha": sha,
                "event_type": types[i],
                "occurred_at": occurred[i],
                "payload": json.loads(payloads[i]),
                "event_id": event_ids[i],
                "correlation_id": corr_ids[i],
                "run_id": run_ids[i],
                "source": source,
                "batch_sha": batch_sha,
                "spooled_at": spooled[i],
                "spool_reason": reasons[i],
            }
            inserted.append({"id": f"row-{sha[:8]}"})
        return inserted


class _Runner(HandlerHookEventCapture):
    """Handler with its two outbound seams substituted, nothing else.

    Both are overridden at the PUBLIC seams the base class exposes (``db``,
    ``get_publish_fn``), never by assigning private attributes, so the
    production call path is what runs. ``get_publish_fn`` returning None is the
    real no-broker case: capture must succeed without a transport.
    """

    def __init__(self, db: FakeConflictAwareDB) -> None:
        self._fake_db = db

    @property
    def db(self) -> Any:
        return self._fake_db

    async def get_publish_fn(self) -> Any:
        return None


@pytest.fixture
def runner() -> tuple[_Runner, FakeConflictAwareDB]:
    db = FakeConflictAwareDB()
    return _Runner(db), db


META = MessageMeta(partition=0, offset=1, fallback_id="f", topic="t")


# ---------------------------------------------------------------------------
# Idempotency — the ticket's acceptance criterion
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_batch_persists_every_distinct_event(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        r, db = runner
        batch = _batch([_event(f"e{i}") for i in range(5)])
        assert await r.project_event("t", batch, META) is True
        assert len(db.rows) == 5

    @pytest.mark.asyncio
    async def test_redelivering_the_same_batch_changes_nothing(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        """The AC: a Kafka replay must not duplicate a single row."""
        r, db = runner
        batch = _batch([_event(f"e{i}") for i in range(5)])
        await r.project_event("t", batch, META)
        before = dict(db.rows)
        await r.project_event("t", batch, META)
        await r.project_event("t", batch, META)
        assert db.rows == before, "replay duplicated or mutated rows"
        assert len(db.rows) == 5

    @pytest.mark.asyncio
    async def test_partial_overlap_inserts_only_the_new_events(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        """A resumed drain re-sends some already-shipped events."""
        r, db = runner
        await r.project_event("t", _batch([_event(f"e{i}") for i in range(3)]), META)
        await r.project_event("t", _batch([_event(f"e{i}") for i in range(1, 6)]), META)
        assert len(db.rows) == 6

    @pytest.mark.asyncio
    async def test_same_event_sha_under_a_different_tenant_is_a_separate_row(
        self,
        runner: tuple[_Runner, FakeConflictAwareDB],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The key is (tenant_id, event_sha), not event_sha alone.

        Two tenants can legitimately capture byte-identical events; collapsing
        them would delete one tenant's history. Both rows must come from the
        HANDLER's own tenant resolution (``house_tenant_write_stamp`` reading
        ``Settings.onex_tenant_id``) -- hand-inserting the second row would
        only prove ``FakeConflictAwareDB``'s own conflict-key comparison, not
        that the handler actually scopes writes by tenant.
        """
        r, db = runner
        _pin_interim_tenant_settings(monkeypatch)
        await r.project_event("t", _batch([_event("shared")]), META)
        monkeypatch.setattr(
            get_settings(), "onex_tenant_id", "other-tenant", raising=True
        )
        await r.project_event("t", _batch([_event("shared")]), META)
        assert len(db.rows) == 2
        assert {tenant for tenant, _ in db.rows} == {"omninode", "other-tenant"}

    @pytest.mark.asyncio
    async def test_writer_stamps_the_rls_guc_explicitly(
        self,
        runner: tuple[_Runner, FakeConflictAwareDB],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OMN-15919: never let the adapter re-derive a tenant of its own.

        The resolved tenant is pinned via monkeypatch (not left to whatever
        the running lane's real env happens to carry) so this test fails for
        a code reason, never because CI exports ``ONEX_TENANT_ID`` or
        ``ENFORCE_TENANT_ISOLATION``.
        """
        r, db = runner
        _pin_interim_tenant_settings(monkeypatch)
        await r.project_event("t", _batch(), META)
        assert db.tenants_seen == ["omninode"]
        assert db.tenants_seen[0] is not None

    @pytest.mark.asyncio
    async def test_whole_batch_is_one_statement(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        """Atomicity: 250 events must not become 250 transactions."""
        r, db = runner
        await r.project_event(
            "t", _batch([_event(f"e{i}") for i in range(MAX_EVENTS_PER_BATCH)]), META
        )
        assert len(db.statements) == 1
        assert len(db.rows) == MAX_EVENTS_PER_BATCH

    @pytest.mark.asyncio
    async def test_conflict_target_matches_the_migration(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        """The SQL's conflict target must be the constraint that exists."""
        r, db = runner
        await r.project_event("t", _batch(), META)
        sql = db.statements[0]
        assert "ON CONFLICT (tenant_id, event_sha) DO NOTHING" in sql
        assert "UNIQUE (tenant_id, event_sha)" in MIGRATION
        assert "DO UPDATE" not in sql, (
            "captured events are immutable history; DO UPDATE would move "
            "updated_at and make a pure replay look like new activity"
        )


# ---------------------------------------------------------------------------
# Malformed input is POISON, not a retry
# ---------------------------------------------------------------------------


class TestMalformedBatchIsPoison:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "batch",
        [
            pytest.param(_batch(events=[]), id="empty-batch"),
            pytest.param(_batch(batch_sha="nope"), id="bad-batch-sha"),
            pytest.param(_batch([_event(event_sha="nope")]), id="bad-event-sha"),
            pytest.param(
                _batch([_event(payload_json="not json")]), id="payload-not-json"
            ),
            pytest.param(
                _batch([_event(payload_json="[1,2]")]), id="payload-not-object"
            ),
            pytest.param(
                _batch(tenant_principal_id="omninode"), id="slug-as-principal"
            ),
            pytest.param(
                _batch([_event(payload_json='{"value": NaN}')]),
                id="payload-json-nan-constant",
            ),
            pytest.param(
                _batch([_event(payload_json='{"value": Infinity}')]),
                id="payload-json-infinity-constant",
            ),
            pytest.param(
                _batch([_event(payload_json='{"value": -Infinity}')]),
                id="payload-json-negative-infinity-constant",
            ),
        ],
    )
    async def test_malformed_batch_raises_rather_than_returning_false(
        self, runner: tuple[_Runner, FakeConflictAwareDB], batch: dict[str, Any]
    ) -> None:
        """Returning False would spin the consumer forever on one offset."""
        r, db = runner
        with pytest.raises(HookEventCaptureError):
            await r.project_event("t", batch, META)
        assert db.rows == {}, "a rejected batch must write nothing"

    @pytest.mark.asyncio
    async def test_caller_supplied_extra_key_is_rejected(
        self, runner: tuple[_Runner, FakeConflictAwareDB]
    ) -> None:
        r, _ = runner
        with pytest.raises(HookEventCaptureError):
            await r.project_event("t", _batch(topic="onex.cmd.attacker.v1"), META)


# ---------------------------------------------------------------------------
# Model shape — the gateway seam
# ---------------------------------------------------------------------------


class TestModelSeam:
    def test_all_four_measured_families_validate(self) -> None:
        """Regression pin against the real corpus census."""
        families = [
            "artifact.captured",
            "tool.output.captured",
            "onex.evt.omniclaude.skill-started.v1",
            "onex.evt.omniclaude.skill-completed.v1",
        ]
        model = ModelHookEventCaptureRequest.model_validate(
            _batch([_event(f"f{i}", event_type=f) for i, f in enumerate(families)])
        )
        assert [e.event_type for e in model.events] == families

    def test_event_id_is_optional(self) -> None:
        """Two of the four measured families carry none at all."""
        assert ModelCapturedHookEvent.model_validate(_event()).event_id is None
        with_id = ModelCapturedHookEvent.model_validate(
            _event(event_id="78a2d73c-05f7-43c9-a924-a818ea56bd55")
        )
        assert with_id.event_id is not None

    def test_tenant_principal_is_required_and_must_not_be_a_slug(self) -> None:
        """payload.tenant_id is attribution-only and must never be the key."""
        batch = _batch()
        del batch["tenant_principal_id"]
        with pytest.raises(ValidationError):
            ModelHookEventCaptureRequest.model_validate(batch)
        with pytest.raises(ValidationError, match="attribution-only"):
            ModelHookEventCaptureRequest.model_validate(
                _batch(tenant_principal_id="omninode")
            )

    def test_tenant_id_slug_stays_optional(self) -> None:
        """It is attribution; a missing slug must not fail the capture."""
        batch = _batch()
        del batch["tenant_id"]
        assert ModelHookEventCaptureRequest.model_validate(batch).tenant_id is None

    def test_batch_size_is_bounded_at_the_consumer_too(self) -> None:
        """Not only at the gateway: the consumer is independently reachable."""
        with pytest.raises(ValidationError):
            ModelHookEventCaptureRequest.model_validate(
                _batch([_event(f"e{i}") for i in range(MAX_EVENTS_PER_BATCH + 1)])
            )

    def test_spool_reason_is_retained(self) -> None:
        """Discarding it destroys the only evidence of WHY events stranded."""
        event = ModelCapturedHookEvent.model_validate(
            _event(
                spool_reason="FileNotFoundError: [Errno 2] No such file or directory"
            )
        )
        assert event.spool_reason is not None
        assert "FileNotFoundError" in event.spool_reason


# ---------------------------------------------------------------------------
# Contract / migration agreement
# ---------------------------------------------------------------------------


class TestContractSeam:
    def test_topics_come_from_the_contract_not_a_literal(self) -> None:
        db = FakeConflictAwareDB()
        r = _Runner(db)
        assert r.topics == CONTRACT["event_bus"]["subscribe_topics"]
        assert r.topics == ["onex.cmd.omnimarket.hook-event-capture-requested.v1"]

    def test_subscribes_to_a_command_topic(self) -> None:
        """It consumes a capture REQUEST, never the event topics themselves."""
        for topic in CONTRACT["event_bus"]["subscribe_topics"]:
            assert ".cmd." in topic

    def test_contract_declares_the_table_this_handler_writes(self) -> None:
        tables = {t["name"] for t in CONTRACT["db_io"]["db_tables"]}
        assert TABLE in tables

    def test_contract_idempotency_hash_fields_match_the_unique_constraint(self) -> None:
        assert CONTRACT["idempotency"]["hash_fields"] == ["tenant_id", "event_sha"]
        assert "UNIQUE (tenant_id, event_sha)" in MIGRATION

    def test_migration_declares_the_migration_file_the_contract_names(self) -> None:
        declared = {t["migration"] for t in CONTRACT["db_io"]["db_tables"]}
        assert "0001_create_hook_events.sql" in declared
        assert (NODE_DIR / "migrations" / "0001_create_hook_events.sql").is_file()

    def test_rls_migration_is_fail_closed(self) -> None:
        assert "ENABLE ROW LEVEL SECURITY" in MIGRATION_RLS
        assert "FORCE ROW LEVEL SECURITY" in MIGRATION_RLS
        assert "current_setting('app.tenant_id', true)" in MIGRATION_RLS

    def test_the_create_migration_carries_no_force_rls(self) -> None:
        """It must stay applyable without an operator fence entry.

        A FORCE-RLS statement anywhere in 0001 makes the whole create
        migration refusable by the forward runner, which would mean the TABLE
        never lands on any lane -- a strictly worse outcome than shipping the
        table with its RLS posture fenced.
        """
        assert "FORCE ROW LEVEL SECURITY" not in MIGRATION
        assert "ENABLE ROW LEVEL SECURITY" not in MIGRATION
        assert "CREATE TABLE IF NOT EXISTS hook_events" in MIGRATION

    def test_migration_carries_the_shape_reconciliation_block(self) -> None:
        """A drifted pre-existing table must converge, not kill the deploy."""
        assert "shape reconciliation" in MIGRATION
        assert "ADD COLUMN IF NOT EXISTS" in MIGRATION
        assert "refuses to guess" in MIGRATION

    def test_optional_columns_are_not_forced_not_null(self) -> None:
        """NULL is the honest value for a producer that emits no event_id."""
        block = MIGRATION.split("FOREACH v_col IN ARRAY ARRAY[")[1].split("]")[0]
        for optional in ("event_id", "correlation_id", "run_id", "spool_reason"):
            assert f"'{optional}'" not in block
