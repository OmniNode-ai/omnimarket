# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the open-obligations projection (OMN-17019 / C9).

The load-bearing tests here are the REPLAY ones. Everything else in this node is
ordinary reducer plumbing; the reason the node is shaped the way it is -- split
``original_owed_by`` / ``transferred_owed_by`` columns, ``closed_state`` written
only by terminal kinds, ``state`` and ``owed_by`` derived in the database -- is
that a consumer restarting from an earlier partition offset re-delivers old
events after newer ones have already been applied. A projection that let a
re-delivered ``created`` reopen a satisfied obligation would look perfectly
healthy while reporting work as owed that was delivered days ago.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_open_obligations.handlers.handler_projection_open_obligations import (
    _COLUMNS_BY_KIND,
    _COMMON_COLUMNS,
    _TOPIC_KIND,
    HandlerProjectionOpenObligations,
    _load_topics,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    REQUIRED_FIELDS_BY_KIND,
    TERMINAL_KINDS,
    EnumObligationEventKind,
    EnumObligationState,
    ModelObligationEventInbound,
    ObligationProjectionError,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NODE_DIR = (
    _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_projection_open_obligations"
)
_TOPICS_YAML = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_emit_daemon"
    / "registries"
    / "topics.yaml"
)

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 8, 30, 13, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 8, 30, 14, 0, 0, tzinfo=UTC)

_TOPIC_BY_KIND: dict[EnumObligationEventKind, str] = {
    kind: topic for topic, kind in _TOPIC_KIND.items()
}


def _created(**overrides: object) -> ModelObligationEventInbound:
    payload: dict[str, object] = {
        "obligation_id": "ob-1",
        "emitted_at": _T0,
        "actor_id": "session-abc",
        "summary": "send the beta readiness brief to the operator",
        "asked_by": "operator",
        "owed_by": "session-abc",
        "acceptance_condition": "brief delivered and acknowledged",
    }
    payload.update(overrides)
    return ModelObligationEventInbound(**payload)  # type: ignore[arg-type]


def _event(
    kind: EnumObligationEventKind, **overrides: object
) -> ModelObligationEventInbound:
    base: dict[str, object] = {
        "obligation_id": "ob-1",
        "emitted_at": _T1,
        "actor_id": "session-abc",
        "summary": f"{kind.value} for ob-1",
    }
    per_kind: dict[EnumObligationEventKind, dict[str, object]] = {
        EnumObligationEventKind.CREATED: {
            "asked_by": "operator",
            "owed_by": "session-abc",
            "acceptance_condition": "brief delivered and acknowledged",
        },
        EnumObligationEventKind.TRANSFERRED: {"owed_by": "session-xyz"},
        EnumObligationEventKind.SATISFIED: {
            "evidence_uri": "https://example.invalid/brief.md",
            "delivery_state": "sent",
        },
        EnumObligationEventKind.SUPERSEDED: {"superseded_by_obligation_id": "ob-2"},
        EnumObligationEventKind.ABANDONED: {
            "abandon_reason": "operator withdrew the ask"
        },
    }
    base.update(per_kind[kind])
    base.update(overrides)
    return ModelObligationEventInbound(**base)  # type: ignore[arg-type]


def _stored(db: InmemoryDatabaseAdapter) -> dict[str, object]:
    rows = db.tables["open_obligations"]
    assert len(rows) == 1, f"expected exactly one folded row, got {len(rows)}"
    return rows[0]


# =============================================================================
# Contract / registry agreement
# =============================================================================


def test_every_declared_topic_maps_to_a_distinct_kind() -> None:
    """Five topics, five kinds, no collisions."""
    assert len(_TOPIC_KIND) == len(EnumObligationEventKind)
    assert set(_TOPIC_KIND.values()) == set(EnumObligationEventKind)


def test_load_topics_refuses_a_contract_with_the_wrong_topic_count(
    tmp_path: Path,
) -> None:
    """A truncated subscribe list must fail at import, not fold events wrongly.

    Topic order is resolved BY POSITION, so a list that lost an entry would
    silently reclassify every event after the gap -- a ``satisfied`` folded as a
    ``superseded``. Refusing on the count is what makes that loud.
    """
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    contract["event_bus"]["subscribe_topics"] = contract["event_bus"][
        "subscribe_topics"
    ][:3]
    truncated = tmp_path / "contract.yaml"
    truncated.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match="exactly 5 subscribe topics"):
        _load_topics(truncated)


def test_required_fields_agree_with_the_emit_registry() -> None:
    """The handler's per-kind requirements must equal the registry's.

    Two surfaces describe the same wire contract: ``REQUIRED_FIELDS_BY_KIND``
    (enforced at projection time) and ``topics.yaml``'s ``required_fields``
    (enforced at emit time). If they drift, an event the emitter accepts is one
    the projection DLQs, and the obligation is lost between the two.
    """
    registry = yaml.safe_load(_TOPICS_YAML.read_text())["events"]
    for kind, required in REQUIRED_FIELDS_BY_KIND.items():
        declared = set(registry[kind.value]["required_fields"])
        # The registry additionally requires the fields common to every kind;
        # the handler gets those from the model's own non-optional fields.
        assert declared == {"obligation_id", "actor_id", "summary"} | set(required), (
            f"{kind.value}: handler requires {sorted(required)}, registry declares "
            f"{sorted(declared)}"
        )


def test_registry_partitions_every_obligation_kind_on_obligation_id() -> None:
    """All five kinds must share one partition key or the fold becomes a race."""
    registry = yaml.safe_load(_TOPICS_YAML.read_text())["events"]
    for kind in EnumObligationEventKind:
        assert registry[kind.value]["partition_key_field"] == "obligation_id"


def test_registry_publishes_every_obligation_kind_as_duty_critical() -> None:
    """Obligation writes fail closed; telemetry tier permits a silent drop."""
    registry = yaml.safe_load(_TOPICS_YAML.read_text())["events"]
    for kind in EnumObligationEventKind:
        tiers = {fan["tier"] for fan in registry[kind.value]["fan_out"]}
        assert tiers == {"duty_critical"}, f"{kind.value} fans out at {tiers}"


def test_obligation_topics_reuse_the_existing_producer_namespace() -> None:
    """No bespoke topic family -- OMN-17019 DoD item 1."""
    for topic in _TOPIC_KIND:
        assert topic.startswith("onex.evt.omniclaude."), topic


# =============================================================================
# Column ownership -- the invariant the replay safety rests on
# =============================================================================


def test_only_terminal_kinds_write_closed_state() -> None:
    for kind, columns in _COLUMNS_BY_KIND.items():
        if kind in TERMINAL_KINDS:
            assert "closed_state" in columns
        else:
            assert "closed_state" not in columns


def test_no_kind_writes_a_derived_column() -> None:
    """``state`` and ``owed_by`` are GENERATED in Postgres. Writing either would
    fail at the database, so catching it here is cheaper than a deploy cycle."""
    for columns in _COLUMNS_BY_KIND.values():
        assert "state" not in columns
        assert "owed_by" not in columns
    assert "state" not in _COMMON_COLUMNS
    assert "owed_by" not in _COMMON_COLUMNS


def test_created_and_transferred_own_different_owner_columns() -> None:
    """The split that stops a replayed ``created`` restoring a stale owner."""
    assert "original_owed_by" in _COLUMNS_BY_KIND[EnumObligationEventKind.CREATED]
    assert (
        "transferred_owed_by" in _COLUMNS_BY_KIND[EnumObligationEventKind.TRANSFERRED]
    )
    assert (
        set(_COLUMNS_BY_KIND[EnumObligationEventKind.CREATED])
        & set(_COLUMNS_BY_KIND[EnumObligationEventKind.TRANSFERRED])
        == set()
    )


def test_created_writes_only_the_columns_its_kind_owns() -> None:
    db = InmemoryDatabaseAdapter()
    HandlerProjectionOpenObligations().project(
        _created(), db, _TOPIC_BY_KIND[EnumObligationEventKind.CREATED]
    )
    expected = set(_COMMON_COLUMNS) | set(
        _COLUMNS_BY_KIND[EnumObligationEventKind.CREATED]
    )
    assert set(_stored(db)) == expected


# =============================================================================
# Replay safety
# =============================================================================


def test_replayed_created_after_satisfied_does_not_reopen_the_obligation() -> None:
    """A consumer restart re-delivers ``created``; the close must survive.

    This is the defect the whole column-ownership split exists to prevent. With
    a writable ``state`` column the third projection below would set it back to
    'open' and the projection would report a delivered obligation as owed.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionOpenObligations()
    handler.project(_created(), db, _TOPIC_BY_KIND[EnumObligationEventKind.CREATED])
    handler.project(
        _event(EnumObligationEventKind.SATISFIED, emitted_at=_T1),
        db,
        _TOPIC_BY_KIND[EnumObligationEventKind.SATISFIED],
    )
    assert _stored(db)["closed_state"] == EnumObligationState.SATISFIED.value

    handler.project(_created(), db, _TOPIC_BY_KIND[EnumObligationEventKind.CREATED])

    row = _stored(db)
    assert row["closed_state"] == EnumObligationState.SATISFIED.value
    assert row["evidence_uri"] == "https://example.invalid/brief.md"


def test_replayed_created_after_transfer_does_not_restore_the_previous_owner() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionOpenObligations()
    handler.project(_created(), db, _TOPIC_BY_KIND[EnumObligationEventKind.CREATED])
    handler.project(
        _event(EnumObligationEventKind.TRANSFERRED),
        db,
        _TOPIC_BY_KIND[EnumObligationEventKind.TRANSFERRED],
    )
    handler.project(_created(), db, _TOPIC_BY_KIND[EnumObligationEventKind.CREATED])

    row = _stored(db)
    # `owed_by` is COALESCE(transferred_owed_by, original_owed_by) in Postgres,
    # so the transferred value still wins even though `created` was re-applied.
    assert row["transferred_owed_by"] == "session-xyz"
    assert row["original_owed_by"] == "session-abc"


def test_double_replay_of_a_whole_lifecycle_is_idempotent() -> None:
    """Replaying the same stream twice must leave the table identical."""
    handler = HandlerProjectionOpenObligations()
    sequence = [
        (EnumObligationEventKind.CREATED, _T0),
        (EnumObligationEventKind.TRANSFERRED, _T1),
        (EnumObligationEventKind.SATISFIED, _T2),
    ]

    first = InmemoryDatabaseAdapter()
    for kind, when in sequence:
        handler.project(_event(kind, emitted_at=when), first, _TOPIC_BY_KIND[kind])
    once = dict(_stored(first))

    for _ in range(2):
        for kind, when in sequence:
            handler.project(_event(kind, emitted_at=when), first, _TOPIC_BY_KIND[kind])
    assert dict(_stored(first)) == once


# =============================================================================
# Fail-closed refusals
# =============================================================================


@pytest.mark.parametrize(
    ("kind", "dropped"),
    [
        (EnumObligationEventKind.CREATED, "acceptance_condition"),
        (EnumObligationEventKind.CREATED, "owed_by"),
        (EnumObligationEventKind.TRANSFERRED, "owed_by"),
        (EnumObligationEventKind.SATISFIED, "evidence_uri"),
        (EnumObligationEventKind.SATISFIED, "delivery_state"),
        (EnumObligationEventKind.SUPERSEDED, "superseded_by_obligation_id"),
        (EnumObligationEventKind.ABANDONED, "abandon_reason"),
    ],
)
def test_missing_required_field_is_refused_not_projected_as_null(
    kind: EnumObligationEventKind, dropped: str
) -> None:
    """Obligation writes fail closed. A NULL here is a lost obligation."""
    event = _event(kind, **{dropped: None})
    with pytest.raises(ObligationProjectionError, match=dropped):
        HandlerProjectionOpenObligations().project(
            event, InmemoryDatabaseAdapter(), _TOPIC_BY_KIND[kind]
        )


def test_satisfied_cannot_close_on_a_ticket_id_alone() -> None:
    """A14: a ticket moving to Done is not evidence anything was delivered."""
    event = _event(
        EnumObligationEventKind.SATISFIED,
        evidence_uri=None,
        delivery_state=None,
        ticket_id="OMN-17019",
    )
    with pytest.raises(ObligationProjectionError, match="evidence_uri"):
        HandlerProjectionOpenObligations().project(
            event,
            InmemoryDatabaseAdapter(),
            _TOPIC_BY_KIND[EnumObligationEventKind.SATISFIED],
        )


def test_undeclared_topic_is_refused() -> None:
    with pytest.raises(ObligationProjectionError, match="not declared"):
        HandlerProjectionOpenObligations().project(
            _created(), InmemoryDatabaseAdapter(), "onex.evt.omniclaude.made-up.v1"
        )


def test_blank_obligation_id_is_rejected_by_the_model() -> None:
    with pytest.raises(ValueError, match="blank or whitespace-only"):
        _created(obligation_id="   ")


def test_emitted_at_has_no_default() -> None:
    """A projection must never invent an event time it was not given."""
    with pytest.raises(ValueError, match="emitted_at"):
        ModelObligationEventInbound(  # type: ignore[call-arg]
            obligation_id="ob-1",
            actor_id="session-abc",
            summary="no emitted_at",
            asked_by="operator",
            owed_by="session-abc",
            acceptance_condition="x",
        )


# =============================================================================
# def-B entrypoint
# =============================================================================


def test_handle_requires_both_db_and_topic() -> None:
    handler = HandlerProjectionOpenObligations()
    with pytest.raises(TypeError, match="DatabaseAdapter"):
        handler.handle({"obligation_id": "ob-1"})
    with pytest.raises(ObligationProjectionError, match="_topic"):
        handler.handle({"_db": InmemoryDatabaseAdapter(), "obligation_id": "ob-1"})


def test_handle_projects_and_reports_the_row_count() -> None:
    db = InmemoryDatabaseAdapter()
    request = _created().model_dump(mode="json")
    request["_db"] = db
    request["_topic"] = _TOPIC_BY_KIND[EnumObligationEventKind.CREATED]
    result = HandlerProjectionOpenObligations().handle(request)
    assert result["rows_upserted"] == 1
    assert result["table"] == "open_obligations"


# =============================================================================
# Migration shape -- asserted statically, because the lane cannot be deployed
# =============================================================================


def _migration_sql() -> str:
    return (_NODE_DIR / "migrations" / "0001_create_open_obligations.sql").read_text()


def test_migration_derives_state_and_owed_by_rather_than_storing_them() -> None:
    sql = _migration_sql()
    assert "state                 TEXT GENERATED ALWAYS AS" in sql
    assert "owed_by               TEXT GENERATED ALWAYS AS" in sql


def test_migration_grants_the_runtime_no_delete() -> None:
    """An obligation leaves the open set only via a recorded terminal event."""
    sql = _migration_sql()
    assert "GRANT SELECT, INSERT, UPDATE ON omninode_internal.open_obligations" in sql
    assert "DELETE ON omninode_internal.open_obligations" not in sql


def test_migration_contains_no_destructive_or_expiring_statement() -> None:
    """No TTL, no sweep, no DROP -- a silent drop is the failure being fixed.

    Comment lines are stripped before the scan. The file's own header explains
    at length why it contains no DROP and no TRUNCATE, and a naive substring
    search over the whole file matches that explanation -- which would make this
    assertion fire on the documentation of the very property it is checking.
    """
    statements = "\n".join(
        line
        for line in _migration_sql().splitlines()
        if not line.strip().startswith("--")
    ).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in statements, forbidden


def test_migration_covers_every_created_column_with_a_guarded_add_column() -> None:
    """The OMN-15376 shape-reconciliation rule, asserted before vendoring.

    The enforcing gate lives in omnibase_infra and only runs on the PR that
    vendors this file. Asserting it here means the vendoring PR is a pure copy
    rather than a place to discover a missing ADD COLUMN one deploy cycle at a
    time.
    """
    sql = _migration_sql()
    body = sql.split("CREATE TABLE IF NOT EXISTS omninode_internal.open_obligations (")[
        1
    ].split(");")[0]
    declared = [
        line.strip().split()[0]
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    assert declared, "no columns parsed out of the CREATE TABLE"
    for column in declared:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, (
            f"{column} has no guarded ADD COLUMN"
        )
