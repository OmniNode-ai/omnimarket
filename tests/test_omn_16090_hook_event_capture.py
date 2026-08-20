# SPDX-License-Identifier: MIT
"""node_hook_event_capture: contract, model and idempotency proofs (OMN-16090).

The load-bearing property is the one the ticket's acceptance criterion names:
persisting N events from a batch and then REDELIVERING the same batch must
leave the row count unchanged. That is proven here through ``handle()`` — the
ONLY entrypoint the real runtime dispatch (projection auto-wiring) ever calls
for a ``db_io`` node — against a fake ``DatabaseAdapter`` that implements the
real single-row ``upsert``/``query`` protocol the injected production adapter
exposes, with the handler's own query-before-upsert guard providing the
``DO NOTHING``-equivalent behavior (see the handler module docstring for why
the injected adapter cannot do this via SQL alone). A mock returning whatever
the test wants would prove nothing about that guard.

The dispatch shape driven here (``handle(dict-with-injected-metadata)``) is
exactly what ``omnibase_infra.runtime.auto_wiring.handler_wiring
._make_projection_dispatch_callback`` builds and calls in production; the
REAL end-to-end wiring proof (constructing that callback for real and driving
it through a ``ModelEventEnvelope``) lives in
``test_omn16090_real_dispatch_path.py``.
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
    CONFLICT_KEY,
    TABLE,
    HandlerHookEventCapture,
    HookEventCaptureError,
)
from omnimarket.nodes.node_hook_event_capture.models.model_hook_event_capture_request import (
    MAX_EVENTS_PER_BATCH,
    ModelCapturedHookEvent,
    ModelHookEventCaptureRequest,
)

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
COMMAND_TOPIC = "onex.cmd.omnimarket.hook-event-capture-requested.v1"


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
    """``DatabaseAdapter`` double: single-row ``upsert``/``query``, no DO-NOTHING.

    Deliberately NOT a MagicMock. It mirrors exactly what the injected
    production adapter offers (one row at a time, no SQL-level DO NOTHING),
    so the property under test — that a replay is a no-op — has to come from
    the HANDLER's own query-before-upsert guard, not from a mock returning a
    canned answer.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []

    def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
        assert table == TABLE
        assert conflict_key == CONFLICT_KEY
        self.upsert_calls.append(dict(row))
        key = (str(row["tenant_id"]), str(row["event_sha"]))
        self.rows[key] = dict(row)
        return True

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert table == TABLE
        self.query_calls.append(dict(filters or {}))
        applied = filters or {}
        return [
            dict(row)
            for row in self.rows.values()
            if all(row.get(k) == v for k, v in applied.items())
        ]


def _dispatch_input(
    batch_payload: dict[str, Any], db: FakeConflictAwareDB
) -> dict[str, Any]:
    """Reproduce EXACTLY what the projection dispatch arm hands to handle().

    ``_make_projection_dispatch_callback`` builds ``input_data`` as the event
    payload plus injected ``_db``/``_event_type``/``_topic`` — see
    ``handler_wiring._callback`` (omnibase_infra).
    """
    return {
        **batch_payload,
        "_db": db,
        "_event_type": "hook-event-capture-requested",
        "_topic": COMMAND_TOPIC,
    }


@pytest.fixture
def runner() -> tuple[HandlerHookEventCapture, FakeConflictAwareDB]:
    db = FakeConflictAwareDB()
    return HandlerHookEventCapture(), db


# ---------------------------------------------------------------------------
# Idempotency — the ticket's acceptance criterion
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_batch_persists_every_distinct_event(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        r, db = runner
        batch = _batch([_event(f"e{i}") for i in range(5)])
        result = r.handle(_dispatch_input(batch, db))
        assert result["events_persisted"] == 5
        assert result["events_already_present"] == 0
        assert result["rows_upserted"] == 5
        assert len(db.rows) == 5

    def test_redelivering_the_same_batch_changes_nothing(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        """The AC: a Kafka replay must not duplicate a single row."""
        r, db = runner
        batch = _batch([_event(f"e{i}") for i in range(5)])
        r.handle(_dispatch_input(batch, db))
        before = dict(db.rows)
        second = r.handle(_dispatch_input(batch, db))
        third = r.handle(_dispatch_input(batch, db))
        assert db.rows == before, "replay duplicated or mutated rows"
        assert len(db.rows) == 5
        assert second["events_persisted"] == 0
        assert second["events_already_present"] == 5
        assert third["events_persisted"] == 0

    def test_redelivery_does_not_touch_updated_at(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        """A replay must not look like new activity (immutable-history intent)."""
        r, db = runner
        batch = _batch([_event("e0")])
        r.handle(_dispatch_input(batch, db))
        upserts_after_first = len(db.upsert_calls)
        r.handle(_dispatch_input(batch, db))
        assert len(db.upsert_calls) == upserts_after_first, (
            "a replayed event must not reach upsert() at all -- the "
            "query-before-upsert guard exists precisely because the "
            "injected adapter's upsert() has no DO NOTHING mode and would "
            "otherwise bump updated_at on every redelivery"
        )

    def test_partial_overlap_inserts_only_the_new_events(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        """A resumed drain re-sends some already-shipped events."""
        r, db = runner
        r.handle(_dispatch_input(_batch([_event(f"e{i}") for i in range(3)]), db))
        result = r.handle(
            _dispatch_input(_batch([_event(f"e{i}") for i in range(1, 6)]), db)
        )
        assert len(db.rows) == 6
        assert result["events_persisted"] == 3
        assert result["events_already_present"] == 2

    def test_same_event_sha_under_a_different_tenant_is_a_separate_row(
        self,
        runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB],
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
        r.handle(_dispatch_input(_batch([_event("shared")]), db))
        monkeypatch.setattr(
            get_settings(), "onex_tenant_id", "other-tenant", raising=True
        )
        r.handle(_dispatch_input(_batch([_event("shared")]), db))
        assert len(db.rows) == 2
        assert {tenant for tenant, _ in db.rows} == {"omninode", "other-tenant"}

    def test_writer_stamps_the_row_tenant_explicitly(
        self,
        runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OMN-15919: the row itself must carry the resolved tenant, not rely
        on the adapter to infer one -- the injected DatabaseAdapter protocol
        has no separate tenant-context channel; the row IS the channel.
        """
        r, db = runner
        _pin_interim_tenant_settings(monkeypatch)
        r.handle(_dispatch_input(_batch(), db))
        assert db.upsert_calls
        assert db.upsert_calls[0]["tenant_id"] == "omninode"

    def test_conflict_target_matches_the_migration(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        """The conflict key this handler uses must be the constraint that exists."""
        assert CONFLICT_KEY == "tenant_id,event_sha"
        assert "UNIQUE (tenant_id, event_sha)" in MIGRATION

    def test_full_batch_size_persists(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        r, db = runner
        result = r.handle(
            _dispatch_input(
                _batch([_event(f"e{i}") for i in range(MAX_EVENTS_PER_BATCH)]), db
            )
        )
        assert result["events_persisted"] == MAX_EVENTS_PER_BATCH
        assert len(db.rows) == MAX_EVENTS_PER_BATCH


# ---------------------------------------------------------------------------
# Malformed input is POISON, not a retry
# ---------------------------------------------------------------------------


class TestMalformedBatchIsPoison:
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
    def test_malformed_batch_raises_rather_than_writing_partial_rows(
        self,
        runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB],
        batch: dict[str, Any],
    ) -> None:
        r, db = runner
        with pytest.raises(HookEventCaptureError):
            r.handle(_dispatch_input(batch, db))
        assert db.rows == {}, "a rejected batch must write nothing"

    def test_caller_supplied_extra_key_is_rejected(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        r, db = runner
        with pytest.raises(HookEventCaptureError):
            r.handle(_dispatch_input(_batch(topic="onex.cmd.attacker.v1"), db))

    def test_non_mapping_input_is_rejected_loudly(
        self, runner: tuple[HandlerHookEventCapture, FakeConflictAwareDB]
    ) -> None:
        """A caller passing the pre-OMN-16090 typed model must fail loudly,
        not silently mis-dispatch -- there is no live caller for that shape
        any more (see the handler module docstring)."""
        r, _db = runner
        with pytest.raises(TypeError):
            r.handle(ModelHookEventCaptureRequest.model_validate(_batch()))


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
        assert CONTRACT["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]

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
