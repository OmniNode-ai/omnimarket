"""OMN-13620: canonical replayable event-chain projection (WS-C Phase 5.1 ROUTE).

The SEA hackathon recorded every event of a generation run to a bespoke JSON
ledger at ``.onex_state/hackathon/event_chains/{correlation_id}.json`` via
``src/pipeline/event_chain_capture.py``. The operator hard-acceptance for this
ROUTE is: given a ``correlation_id``, the ordered event chain must reconstruct
deterministically from *canonical* events landed in a queryable projection — no
bespoke SEA store.

``node_projection_traces`` only materializes an aggregated one-row-per-correlation
summary; it cannot reconstruct the ordered per-event chain. These tests pin a new
canonical REDUCER ``node_projection_event_chain`` that materializes one durable
row per ``(correlation_id, sequence)`` ordered event, exposed read-side via the
``/projection/{topic}`` pattern with a ``correlation_id`` filter and ``sequence``
ordering — the deterministic replay surface.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

NODE_DIR = Path("src/omnimarket/nodes/node_projection_event_chain")
PROJECTION_TOPIC = (
    "onex.snapshot.projection.event_chain.v1"  # onex-topic-allow: snapshot prefix
)
SUBSCRIBE_TOPIC = "onex.evt.platform.log-entry.v1"
TABLE = "event_chain"

# Ordered per-event row shape the replay surface must carry.
REQUIRED_ROW_FIELDS = (
    "correlation_id",
    "sequence",
    "topic",
    "source_node",
    "envelope_id",
    "causation_id",
    "captured_at",
    "payload",
)


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def _handler_module() -> object:
    return import_module(
        "omnimarket.nodes.node_projection_event_chain.handlers."
        "handler_projection_event_chain"
    )


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_contract_is_reducer_with_projection_api() -> None:
    contract = _contract()
    assert contract["name"] == "node_projection_event_chain"
    assert contract["node_type"] == "REDUCER_GENERIC"
    projection_api = contract["projection_api"]
    assert projection_api["expose"] is True
    assert projection_api["topic"] == PROJECTION_TOPIC
    assert projection_api["table"] == TABLE
    assert projection_api["schema"] == "public"


def test_projection_api_exposes_correlation_id_for_replay_filter() -> None:
    """The /projection/{topic} read API only honours a ``correlation_id`` WHERE
    filter when the column is declared verbatim. Replay-by-correlation_id is the
    operator hard-acceptance criterion, so the column MUST be declared."""
    columns = _contract()["projection_api"]["columns"]
    assert "correlation_id" in columns, (
        "projection_api must declare correlation_id so the read API supports the "
        "replay filter (api_server.topic_supports_correlation_id_filter)"
    )
    for required in REQUIRED_ROW_FIELDS:
        assert required in columns, f"projection_api missing replay column {required}"


def test_projection_api_orders_by_sequence_ascending_for_deterministic_replay() -> None:
    projection_api = _contract()["projection_api"]
    order_by = str(projection_api["order_by"])
    assert "sequence" in order_by, "ordered replay must order by sequence"
    assert "ASC" in order_by.upper(), (
        "deterministic chain reconstruction requires ascending sequence order"
    )


def test_event_bus_subscribes_canonical_log_source() -> None:
    event_bus = _contract()["event_bus"]
    assert SUBSCRIBE_TOPIC in event_bus["subscribe_topics"], (
        "must subscribe to the canonical platform log-entry bus source"
    )
    assert event_bus["publish_topics"], "must publish a chain-applied snapshot event"
    assert event_bus["consumer_group"]


def test_contract_declares_db_io_write_so_runtime_wires_materialization() -> None:
    """The runtime auto-wiring projection callback only injects ``_db`` and calls
    handle() for contracts that declare ``db_io.db_tables`` (OMN-13083 root
    cause). A projection_api block alone is read-side only."""
    db_io = _contract()["db_io"]
    tables = [t for t in db_io["db_tables"] if t["name"] == TABLE]
    assert len(tables) == 1, "db_io must declare the event_chain table"
    table = tables[0]
    assert table["access"] == "write"
    assert table["database"] == "omnidash_analytics"
    assert table["migration"] == "0001_create_event_chain.sql"


def test_idempotency_conflict_key_is_correlation_and_envelope() -> None:
    """Replaying the same canonical event (same envelope_id) must not duplicate a
    chain row — the conflict key dedups on (correlation_id, envelope_id)."""
    idempotency = _contract()["idempotency"]
    assert idempotency["enabled"] is True
    hash_fields = idempotency["hash_fields"]
    assert "correlation_id" in hash_fields
    assert "envelope_id" in hash_fields


def test_node_is_discoverable_as_entry_point() -> None:
    eps = [
        ep
        for ep in entry_points(group="onex.nodes")
        if ep.name == "node_projection_event_chain"
        and ep.dist.metadata["Name"] == "omnimarket"
    ]
    assert len(eps) == 1
    assert eps[0].value == "omnimarket.nodes.node_projection_event_chain"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_node_owned_migration_creates_backing_table() -> None:
    migration = NODE_DIR / "migrations" / "0001_create_event_chain.sql"
    sql = migration.read_text()
    assert f"CREATE TABLE IF NOT EXISTS {TABLE}" in sql
    assert "correlation_id" in sql
    assert "sequence" in sql
    assert "envelope_id" in sql
    assert "payload" in sql
    # Composite uniqueness on (correlation_id, envelope_id) for replay-safe dedup.
    assert "correlation_id, envelope_id" in sql or "correlation_id,envelope_id" in sql


# ---------------------------------------------------------------------------
# Handler / runtime path
# ---------------------------------------------------------------------------


def test_handler_topics_are_contract_derived_no_literals() -> None:
    module = _handler_module()
    contract = _contract()
    assert tuple(contract["event_bus"]["subscribe_topics"]) == module.SUBSCRIBE_TOPICS
    assert tuple(contract["event_bus"]["publish_topics"]) == module.PUBLISH_TOPICS
    handler_source = Path(module.__file__).read_text(encoding="utf-8")
    assert "onex.evt." not in handler_source
    assert "onex.snapshot." not in handler_source


def test_handle_without_db_raises_typeerror() -> None:
    module = _handler_module()
    handler = module.HandlerProjectionEventChain()
    with pytest.raises(TypeError):
        handler.handle(
            {
                "correlation_id": "corr-no-db",
                "envelope_id": "env-1",
                "topic": SUBSCRIBE_TOPIC,
            }
        )


def test_handle_materializes_one_ordered_event_row() -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    module = _handler_module()
    db = InmemoryDatabaseAdapter()
    handler = module.HandlerProjectionEventChain()
    result = handler.handle(
        {
            "correlation_id": "corr-1",
            "envelope_id": "env-1",
            "causation_id": "corr-1",
            "topic": "onex.cmd.omnimarket.node-generation-requested.v1",
            "source_node": "demo_script",
            "message": "started",
            "timestamp": "2026-06-24T12:00:00Z",
            "payload": {"task_description": "classify"},
            "_db": db,
            "_event_type": "log-entry",
        }
    )
    assert result["rows_upserted"] == 1
    rows = db.query(TABLE, {"correlation_id": "corr-1"})
    assert len(rows) == 1
    row = rows[0]
    for field in REQUIRED_ROW_FIELDS:
        assert field in row, f"materialized row missing replay field {field}"
    assert row["correlation_id"] == "corr-1"
    assert row["sequence"] == 0
    assert row["envelope_id"] == "env-1"
    assert row["source_node"] == "demo_script"


def test_sequence_is_monotonic_per_correlation() -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    module = _handler_module()
    db = InmemoryDatabaseAdapter()
    handler = module.HandlerProjectionEventChain()
    for i, env in enumerate(("env-a", "env-b", "env-c")):
        handler.handle(
            {
                "correlation_id": "corr-seq",
                "envelope_id": env,
                "topic": f"onex.evt.omnimarket.step-{i}.v1",
                "source_node": f"node_{i}",
                "timestamp": f"2026-06-24T12:00:0{i}Z",
                "payload": {"i": i},
                "_db": db,
                "_event_type": "log-entry",
            }
        )
    rows = sorted(
        db.query(TABLE, {"correlation_id": "corr-seq"}),
        key=lambda r: r["sequence"],
    )
    assert [r["sequence"] for r in rows] == [0, 1, 2]
    assert [r["envelope_id"] for r in rows] == ["env-a", "env-b", "env-c"]


def test_replay_same_envelope_is_idempotent() -> None:
    """Replaying the same canonical event must not append a duplicate row nor
    advance the sequence — proving replay-safety."""
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    module = _handler_module()
    db = InmemoryDatabaseAdapter()
    handler = module.HandlerProjectionEventChain()
    event = {
        "correlation_id": "corr-dup",
        "envelope_id": "env-dup",
        "topic": "onex.evt.omnimarket.x.v1",
        "source_node": "node_x",
        "timestamp": "2026-06-24T12:00:00Z",
        "payload": {},
        "_db": db,
        "_event_type": "log-entry",
    }
    handler.handle(dict(event))
    handler.handle(dict(event))
    rows = db.query(TABLE, {"correlation_id": "corr-dup"})
    assert len(rows) == 1, "same envelope_id must dedup to a single chain row"
    assert rows[0]["sequence"] == 0


def test_chain_reconstructs_deterministically_by_correlation_id() -> None:
    """Operator hard-acceptance: given a correlation_id, the ordered chain
    reconstructs deterministically from the canonical projection rows."""
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    module = _handler_module()
    db = InmemoryDatabaseAdapter()
    handler = module.HandlerProjectionEventChain()

    chain = [
        ("env-0", "onex.cmd.omnimarket.node-generation-requested.v1", "orchestrator"),
        ("env-1", "onex.evt.omnimarket.node-generation-completed.v1", "consumer"),
        ("env-2", "onex.evt.omnimarket.node-deployment-completed.v1", "deployer"),
    ]
    # Interleave a second correlation to prove filtering isolates the chain.
    handler.handle(
        {
            "correlation_id": "other",
            "envelope_id": "other-0",
            "topic": "onex.evt.omnimarket.noise.v1",
            "source_node": "noise",
            "timestamp": "2026-06-24T12:00:00Z",
            "payload": {},
            "_db": db,
            "_event_type": "log-entry",
        }
    )
    for i, (env, topic, node) in enumerate(chain):
        handler.handle(
            {
                "correlation_id": "corr-replay",
                "envelope_id": env,
                "causation_id": chain[i - 1][0] if i else "corr-replay",
                "topic": topic,
                "source_node": node,
                "timestamp": f"2026-06-24T12:0{i}:00Z",
                "payload": {"step": i},
                "_db": db,
                "_event_type": "log-entry",
            }
        )

    # Reconstruct exactly as the read API does: filter by correlation_id, order
    # by sequence ascending.
    reconstructed = sorted(
        db.query(TABLE, {"correlation_id": "corr-replay"}),
        key=lambda r: r["sequence"],
    )
    assert [r["topic"] for r in reconstructed] == [c[1] for c in chain]
    assert [r["source_node"] for r in reconstructed] == [c[2] for c in chain]
    assert [r["sequence"] for r in reconstructed] == [0, 1, 2]
    # Determinism: a second reconstruction yields the identical ordered chain.
    again = sorted(
        db.query(TABLE, {"correlation_id": "corr-replay"}),
        key=lambda r: r["sequence"],
    )
    assert [r["envelope_id"] for r in again] == [c[0] for c in chain]
