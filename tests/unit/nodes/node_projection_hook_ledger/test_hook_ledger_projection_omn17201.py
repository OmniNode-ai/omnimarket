# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17201 -- leg 5: the cloud-side hook-event ledger projection.

RED-first. Every assertion here names the acceptance criterion or the defect
class it pins, so a later reader can tell a real guard from decoration.

The sink is ``public.hook_events`` -- deliberately NOT a new table and
deliberately NOT ``event_ledger``:

* ``event_ledger`` is prohibited by this ticket's own scope (a hand-maintained
  26-topic allowlist of which 6 ever produced a row, and an in-tree record of
  holding zero rows while 1,028,463 events flowed past -- OMN-16176).
* A NEW table would be the "parallel schema" this ticket's scope forbids, and
  it would be unreadable: the cloud read route that AC3's probe consumes
  (``GET /v1/projections/hook-events/by-correlation``, omninode_infra
  ``docker/onex-api/routers/ledger_projection.py``, OMN-17205) already selects
  from ``hook_events`` and today answers ``projection_absent``. Leg 5 is the
  writer that route was built waiting for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_hook_ledger"
    / "contract.yaml"
)

CANONICAL_HOOK_TOPICS = (
    "onex.evt.omniclaude.session-started.v1",
    "onex.evt.omniclaude.prompt-submitted.v1",
    "onex.evt.omniclaude.tool-executed.v1",
    "onex.evt.omniclaude.session-ended.v1",
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    with open(CONTRACT_PATH) as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict)
    return loaded


# ---------------------------------------------------------------------------
# AC1 -- the contract declares the hook-event topics and its table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_every_governed_hook_topic(contract: dict[str, Any]) -> None:
    declared = list(contract["event_bus"]["subscribe_topics"])
    assert declared == list(CANONICAL_HOOK_TOPICS)


@pytest.mark.unit
def test_contract_declares_hook_events_as_its_only_table(
    contract: dict[str, Any],
) -> None:
    tables = contract["db_io"]["db_tables"]
    assert [t["name"] for t in tables] == ["hook_events"]
    assert tables[0]["schema"] == "public"
    assert tables[0]["access"] == "write"


@pytest.mark.unit
def test_contract_never_declares_the_prohibited_event_ledger_table(
    contract: dict[str, Any],
) -> None:
    """This ticket's scope forbids event_ledger by name."""
    names = {t["name"] for t in contract["db_io"]["db_tables"]}
    assert "event_ledger" not in names


@pytest.mark.unit
def test_contract_reuses_the_owning_nodes_migration_and_invents_no_parallel_table(
    contract: dict[str, Any],
) -> None:
    """No second migration for a table another node already owns.

    Same shape node_projection_tenant_credentials already uses for
    delegation_routing_tenant_overlay: a relative path into the owner.
    """
    migration = contract["db_io"]["db_tables"][0]["migration"]
    assert migration == (
        "../node_hook_event_capture/migrations/0001_create_hook_events.sql"
    )
    resolved = (CONTRACT_PATH.parent / migration).resolve()
    assert resolved.is_file(), f"declared migration does not exist: {resolved}"


@pytest.mark.unit
def test_contract_exposes_the_projection_for_reads(contract: dict[str, Any]) -> None:
    assert contract["projection_api"]["expose"] is True
    assert contract["projection_api"]["table"] == "hook_events"


@pytest.mark.unit
def test_contract_exposure_carries_correlation_id_so_the_ac3_probe_can_read_it(
    contract: dict[str, Any],
) -> None:
    """AC3 reads back BY CORRELATION ID. An exposure without it cannot serve that."""
    assert "correlation_id" in contract["projection_api"]["columns"]


# ---------------------------------------------------------------------------
# The cloud wire scope -- contract-declared, never env-sourced
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_a_non_empty_cloud_tenant_wire_scope(
    contract: dict[str, Any],
) -> None:
    slugs = contract["config"]["hook_ledger"]["cloud_wire_scope"]["tenant_slugs"]
    assert isinstance(slugs, list)
    assert slugs, "wire scope must not be empty"
    assert all(isinstance(s, str) and s for s in slugs)


@pytest.mark.unit
def test_no_env_var_supplies_the_tenant_wire_scope() -> None:
    """Config comes from the contract. A new env var would be a second authority."""
    from omnimarket.nodes.node_projection_hook_ledger.handlers import (
        handler_hook_ledger_projection as mod,
    )

    source = Path(mod.__file__).read_text()
    assert "TENANT_SLUG" not in source
    assert "HOOK_LEDGER_TENANT" not in source


# ---------------------------------------------------------------------------
# Physical wire-topic resolution -- through the single resolver (OMN-15792)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runner_subscribes_to_tenant_prefixed_wire_topics_not_bare_canonical() -> None:
    """The cloud bus carries tenant-{slug}. topics; a bare subscribe reads nothing."""
    from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
        HandlerHookLedgerProjection,
    )

    topics = HandlerHookLedgerProjection.resolve_wire_topics(
        ["beta-gateway-canary"], CANONICAL_HOOK_TOPICS
    )
    assert topics == [f"tenant-beta-gateway-canary.{t}" for t in CANONICAL_HOOK_TOPICS]


@pytest.mark.unit
def test_an_empty_tenant_wire_scope_is_refused_rather_than_subscribing_to_nothing() -> (
    None
):
    """A silently-empty subscribe list is a writer that reports healthy and reads nothing."""
    from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
        HandlerHookLedgerProjection,
    )

    with pytest.raises(ValueError, match="tenant_slugs"):
        HandlerHookLedgerProjection.resolve_wire_topics([], CANONICAL_HOOK_TOPICS)


@pytest.mark.unit
def test_a_reserved_tenant_slug_is_refused_by_the_shared_resolver() -> None:
    from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
        HandlerHookLedgerProjection,
    )

    with pytest.raises(ValueError, match="reserved"):
        HandlerHookLedgerProjection.resolve_wire_topics(
            ["system"], CANONICAL_HOOK_TOPICS
        )


# ---------------------------------------------------------------------------
# Row derivation
# ---------------------------------------------------------------------------


def _envelope_record(
    *,
    correlation_id: str = "corr-abc123",
    emitted_at: str = "2026-09-05T04:00:00+00:00",
    tenant_slug: str = "beta-gateway-canary",
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The shape the cloud bus actually carries: an unwrapped ModelEventEnvelope.

    ``unwrap_envelope`` hands the handler the PAYLOAD with the full envelope
    re-attached under ``_envelope`` -- so both halves are available here.
    """
    payload: dict[str, Any] = {
        "hook_source": "user_prompt_submit",
        "prompt_length": 4242,
        "session_id": "sess-1",
        "correlation_id": correlation_id,
        "causation_id": None,
        "emitted_at": emitted_at,
        "redaction_state": "redacted",
    }
    payload.update(extra_payload or {})
    return {
        **payload,
        "_envelope": {
            "envelope_id": "env-1",
            "correlation_id": correlation_id,
            "event_type": "prompt-submitted",
            "payload": payload,
            "metadata": {
                "tags": {
                    "gateway_tenant_slug": tenant_slug,
                    "gateway_direction": "local-to-cloud",
                }
            },
        },
    }


WIRE_TOPIC = "tenant-beta-gateway-canary.onex.evt.omniclaude.prompt-submitted.v1"


def _row(**kw: Any) -> dict[str, Any]:
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        derive_hook_ledger_row,
    )

    params: dict[str, Any] = {
        "wire_topic": WIRE_TOPIC,
        "data": _envelope_record(),
        "partition": 3,
        "offset": 77,
    }
    params.update(kw)
    return derive_hook_ledger_row(**params)


@pytest.mark.unit
def test_event_sha_is_a_sha256_hex_digest() -> None:
    import re

    assert re.fullmatch(r"[0-9a-f]{64}", _row()["event_sha"])


@pytest.mark.unit
def test_the_same_event_derives_the_same_event_sha_so_replay_is_idempotent() -> None:
    """The UNIQUE (tenant_id, event_sha) key is what makes a redelivery a no-op."""
    assert _row()["event_sha"] == _row()["event_sha"]


@pytest.mark.unit
def test_a_different_event_derives_a_different_event_sha() -> None:
    other = _row(data=_envelope_record(correlation_id="corr-different"))
    assert _row()["event_sha"] != other["event_sha"]


@pytest.mark.unit
def test_event_sha_does_not_depend_on_the_delivery_coordinates() -> None:
    """A rebalance re-reading the same record at a new offset must not duplicate it."""
    assert (
        _row(partition=0, offset=1)["event_sha"]
        == _row(partition=9, offset=999)["event_sha"]
    )


@pytest.mark.unit
def test_correlation_id_is_carried_onto_the_row_because_ac3_reads_by_it() -> None:
    assert _row()["correlation_id"] == "corr-abc123"


@pytest.mark.unit
def test_occurred_at_is_the_producers_own_timestamp_never_ingest_time() -> None:
    """The migration is explicit: stamping ingest time destroys the only ordering signal."""
    row = _row(data=_envelope_record(emitted_at="2026-09-05T04:00:00+00:00"))
    assert row["occurred_at"].isoformat() == "2026-09-05T04:00:00+00:00"


@pytest.mark.unit
def test_a_record_with_no_producer_timestamp_is_refused_not_stamped_with_now() -> None:
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        HookLedgerProjectionError,
    )

    record = _envelope_record()
    del record["emitted_at"]
    del record["_envelope"]["payload"]["emitted_at"]
    with pytest.raises(HookLedgerProjectionError, match="emitted_at"):
        _row(data=record)


@pytest.mark.unit
def test_tenant_id_is_derived_from_the_wire_topic_not_from_the_payload() -> None:
    """OMN-17066 class: a payload-sourced tenant stamp collapses tenants on one key."""
    record = _envelope_record()
    record["tenant_id"] = "some-other-tenant"
    record["_envelope"]["payload"]["tenant_id"] = "some-other-tenant"
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        HookLedgerProjectionError,
    )

    with pytest.raises(HookLedgerProjectionError, match="tenant"):
        _row(data=record)


@pytest.mark.unit
def test_a_gateway_tenant_tag_that_disagrees_with_the_wire_topic_is_refused() -> None:
    """Two tenant authorities that disagree is a fail-closed refusal, never a pick."""
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        HookLedgerProjectionError,
    )

    with pytest.raises(HookLedgerProjectionError, match="tenant"):
        _row(data=_envelope_record(tenant_slug="a-different-tenant"))


@pytest.mark.unit
def test_the_wire_topic_tenant_becomes_the_row_tenant() -> None:
    assert _row()["tenant_id"] == "beta-gateway-canary"


@pytest.mark.unit
def test_a_bare_untenanted_wire_topic_is_refused() -> None:
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        HookLedgerProjectionError,
    )

    with pytest.raises(HookLedgerProjectionError, match="tenant"):
        _row(wire_topic="onex.evt.omniclaude.prompt-submitted.v1")


@pytest.mark.unit
def test_event_type_is_the_canonical_topic_with_the_tenant_prefix_stripped() -> None:
    assert _row()["event_type"] == "onex.evt.omniclaude.prompt-submitted.v1"


@pytest.mark.unit
def test_batch_sha_is_a_sha256_of_the_delivery_coordinates() -> None:
    import hashlib
    import re

    row = _row(partition=3, offset=77)
    assert re.fullmatch(r"[0-9a-f]{64}", row["batch_sha"])
    assert row["batch_sha"] == hashlib.sha256(f"{WIRE_TOPIC}:3:77".encode()).hexdigest()


@pytest.mark.unit
def test_source_marks_the_relay_so_relay_rows_are_distinguishable_from_spool_rows() -> (
    None
):
    """node_hook_event_capture writes the same table from the retiring spool path."""
    assert _row()["source"] == "gateway-relay"


@pytest.mark.unit
def test_payload_is_stored_verbatim_without_the_runners_synthetic_keys() -> None:
    payload = _row()["payload"]
    assert payload["hook_source"] == "user_prompt_submit"
    assert payload["prompt_length"] == 4242
    assert not any(k.startswith("_") for k in payload), payload


@pytest.mark.unit
def test_a_flat_non_envelope_record_is_refused_by_name_citing_omn17919() -> None:
    """The stability lane carries FLAT hook records; the cloud bus carries envelopes.

    Conflating the two silently is exactly the OMN-17919 defect one leg up.
    """
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        HookLedgerProjectionError,
    )

    flat = _envelope_record()
    del flat["_envelope"]
    with pytest.raises(HookLedgerProjectionError, match="envelope"):
        _row(data=flat)


# ---------------------------------------------------------------------------
# Runner behaviour
# ---------------------------------------------------------------------------


class _FakeDb:
    """Records statements. ``returns_row`` False models a suppressed duplicate."""

    def __init__(self, *, returns_row: bool = True) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []
        self.tenants: list[str | None] = []
        self._returns_row = returns_row

    async def execute(
        self, sql: str, *args: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        self.rows.append((sql, args))
        self.tenants.append(tenant)
        if not self._returns_row:
            return []
        return [{"event_sha": args[1] if len(args) > 1 else None}]


def _runner(
    *, returns_row: bool = True
) -> tuple[Any, _FakeDb, list[tuple[str, bytes]]]:
    from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
        HandlerHookLedgerProjection,
    )

    runner = HandlerHookLedgerProjection.__new__(HandlerHookLedgerProjection)
    runner._load_contract()  # type: ignore[attr-defined]
    db = _FakeDb(returns_row=returns_row)
    runner._db = db  # type: ignore[attr-defined]
    published: list[tuple[str, bytes]] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    runner._publish_fn = _publish  # type: ignore[attr-defined]
    return runner, db, published


@pytest.mark.unit
def test_runner_topics_are_the_resolved_wire_topics_from_the_contract() -> None:
    topics = _runner()[0].topics
    assert topics
    assert all(t.startswith("tenant-") for t in topics)
    assert len(topics) == len(CANONICAL_HOOK_TOPICS)


@pytest.mark.unit
def test_project_event_writes_exactly_one_row_on_the_contract_conflict_key() -> None:
    import asyncio

    from omnimarket.projection.runner import MessageMeta

    runner, db, published = _runner()
    meta = MessageMeta(partition=3, offset=77, fallback_id="f", topic=WIRE_TOPIC)
    ok = asyncio.run(runner.project_event(WIRE_TOPIC, _envelope_record(), meta))
    assert ok is True
    assert len(db.rows) == 1
    sql = db.rows[0][0]
    assert "hook_events" in sql
    assert "ON CONFLICT (tenant_id, event_sha)" in sql
    assert [t for t, _ in published] == [
        "onex.evt.omnimarket.projection-hook-ledger-applied.v1"
    ]


@pytest.mark.unit
def test_a_suppressed_duplicate_publishes_no_applied_event() -> None:
    """The applied-event asserts a DURABLE ROW LANDED, not that handle() ran.

    Without this, a redelivery that the table's UNIQUE key correctly suppressed
    would still assert that a row landed -- degrading the terminal event into
    "the handler did not raise", which is the OMN-13360 defect class.
    """
    import asyncio

    from omnimarket.projection.runner import MessageMeta

    runner, db, published = _runner(returns_row=False)
    meta = MessageMeta(partition=3, offset=77, fallback_id="f", topic=WIRE_TOPIC)
    ok = asyncio.run(runner.project_event(WIRE_TOPIC, _envelope_record(), meta))
    assert ok is True
    assert len(db.rows) == 1
    assert published == []


@pytest.mark.unit
def test_the_write_sets_the_rls_tenant_guc_or_force_rls_refuses_every_row() -> None:
    """public.hook_events is FORCE ROW LEVEL SECURITY with a fail-closed policy.

    Without app.tenant_id on the same transaction the WITH CHECK predicate
    compares against NULL and the INSERT is refused -- every row, silently.
    FORCE means even the table owner is not exempt. OMN-15301 is the recorded
    instance of a projection writer that never set it.
    """
    import asyncio

    from omnimarket.projection.runner import MessageMeta

    runner, db, _published = _runner()
    meta = MessageMeta(partition=3, offset=77, fallback_id="f", topic=WIRE_TOPIC)
    asyncio.run(runner.project_event(WIRE_TOPIC, _envelope_record(), meta))
    assert db.tenants == ["beta-gateway-canary"]


@pytest.mark.unit
def test_the_exposure_declares_no_tenant_column_because_it_is_not_bus_backed(
    contract: dict[str, Any],
) -> None:
    """The pair is illegal together, and the DB is the scoping surface here.

    ProjectionTableConfig hard-refuses tenant_column on a non-bus_backed
    exposure, and that refusal raises out of build_projection_topic_map -- it
    would take every exposure in the repo down, not just this one.
    """
    exposure = contract["projection_api"]
    assert exposure["bus_backed"] is False
    assert "tenant_column" not in exposure


@pytest.mark.unit
def test_the_node_declares_a_poison_dlq_so_one_bad_record_cannot_wedge_the_leg() -> (
    None
):
    """OMN-17382: without a DLQ the base runner re-raises and never commits."""
    runner, _db, _published = _runner()
    assert runner.poison_dlq_topics == [
        "onex.dlq.omnimarket.projection-hook-ledger-malformed.v1"
    ]


@pytest.mark.unit
def test_a_topic_outside_the_declared_wire_set_is_never_projected() -> None:
    import asyncio

    from omnimarket.projection.runner import MessageMeta

    runner, db, _published = _runner()
    foreign = "tenant-beta-gateway-canary.onex.evt.omniclaude.tool-output-captured.v1"
    meta = MessageMeta(partition=0, offset=0, fallback_id="f", topic=foreign)
    ok = asyncio.run(runner.project_event(foreign, _envelope_record(), meta))
    assert ok is False
    assert db.rows == []


# ---------------------------------------------------------------------------
# OMN-14355 -- the node is born canonical (definition B)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_is_the_canonical_definition_b_shape_not_a_dict_in_dict_out() -> None:
    """A new node must be born canonical; the 95 baselined siblings are debt.

    The ratchet's baseline may only SHRINK, so adding this node to it would be
    using an allowlist as a fix. This pins the shape instead.
    """
    import inspect

    from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
        HandlerHookLedgerProjection,
    )
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        ModelHookLedgerProjectionRequest,
        ModelHookLedgerProjectionResult,
    )

    hints = inspect.get_annotations(HandlerHookLedgerProjection.handle, eval_str=True)
    assert hints["request"] is ModelHookLedgerProjectionRequest
    assert hints["return"] is ModelHookLedgerProjectionResult


@pytest.mark.unit
def test_handle_reports_the_real_row_count_not_an_inferred_one() -> None:
    """rows_upserted must fall to 0 for a duplicate the UNIQUE key suppressed.

    Inferring it from "handle() did not raise" is the OMN-13360 defect the
    terminal applied-event exists to avoid.
    """
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        ModelHookLedgerProjectionRequest,
    )

    request = ModelHookLedgerProjectionRequest(
        wire_topic=WIRE_TOPIC, record=_envelope_record(), partition=3, offset=77
    )

    runner, _db, published = _runner()
    fresh = runner.handle(request)
    assert fresh.projected is True
    assert fresh.rows_upserted == 1
    assert fresh.correlation_id == "corr-abc123"
    assert len(published) == 1

    dup_runner, _dup_db, dup_published = _runner(returns_row=False)
    dup = dup_runner.handle(request)
    assert dup.rows_upserted == 0
    assert dup_published == []


@pytest.mark.unit
def test_handle_refuses_a_topic_outside_the_declared_wire_set() -> None:
    from omnimarket.nodes.node_projection_hook_ledger.models.model_hook_ledger_event import (
        ModelHookLedgerProjectionRequest,
    )

    runner, db, _published = _runner()
    result = runner.handle(
        ModelHookLedgerProjectionRequest(
            wire_topic=(
                "tenant-beta-gateway-canary.onex.evt.omniclaude.tool-output-captured.v1"
            ),
            record=_envelope_record(),
            partition=0,
            offset=0,
        )
    )
    assert result.projected is False
    assert result.rows_upserted == 0
    assert db.rows == []
