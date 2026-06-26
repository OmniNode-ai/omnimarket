"""Golden chain tests for node_projection_event_chain (OMN-13620).

Covers the event-chain projection consume-leg: canonical platform log-entry
events -> ordered per-(correlation_id, sequence) ``event_chain`` rows, replacing
the bespoke SEA EventChainCapture JSON ledger. Includes the row-delta proof
pattern (before=0, project one event, after=exactly 1 row), monotonic sequence
assignment, replay idempotency (same envelope_id dedups, sequence stable), and
the operator hard-acceptance: given a correlation_id, the ordered chain
reconstructs deterministically from the canonical projection rows.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_event_chain.handlers.handler_projection_event_chain import (
    HandlerProjectionEventChain,
)
from omnimarket.nodes.node_projection_event_chain.models.model_event_chain_event import (
    ModelEventChainProjectionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionEventChain()
_TABLE = "event_chain"
_CORR = "corr-golden-chain-001"
_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_event_chain/contract.yaml"


def _event(envelope_id: str, **overrides: object) -> ModelEventChainProjectionEvent:
    base: dict[str, object] = {
        "correlation_id": _CORR,
        "envelope_id": envelope_id,
        "topic": "onex.evt.omnimarket.step.v1",  # onex-topic-allow: test fixture literal
        "source_node": "node_consumer",
        "timestamp": "2026-06-24T12:00:00Z",
        "payload": {},
    }
    base.update(overrides)
    return ModelEventChainProjectionEvent(**base)


class TestEventChainProjectionChain:
    def test_row_delta_before_zero_after_one(self) -> None:
        """Row-delta proof: before=0, project one event, after=1 row."""
        db = InmemoryDatabaseAdapter()
        assert len(db.query(_TABLE)) == 0

        result = HANDLER.project(_event("env-0"), db)

        assert result.rows_upserted == 1
        rows = db.query(_TABLE)
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == _CORR
        assert rows[0]["sequence"] == 0
        assert rows[0]["envelope_id"] == "env-0"

    def test_sequence_monotonic_within_correlation(self) -> None:
        db = InmemoryDatabaseAdapter()
        for env in ("env-0", "env-1", "env-2"):
            HANDLER.project(_event(env), db)
        rows = sorted(
            db.query(_TABLE, {"correlation_id": _CORR}), key=lambda r: r["sequence"]
        )
        assert [r["sequence"] for r in rows] == [0, 1, 2]

    def test_replay_same_envelope_is_idempotent(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_event("env-dup"), db)
        HANDLER.project(_event("env-dup"), db)
        rows = db.query(_TABLE, {"correlation_id": _CORR})
        assert len(rows) == 1, "same envelope_id must dedup to one row"
        assert rows[0]["sequence"] == 0

    def test_distinct_correlations_produce_distinct_chains(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_event("env-x", correlation_id="corr-x"), db)
        HANDLER.project(_event("env-y", correlation_id="corr-y"), db)
        assert len(db.query(_TABLE, {"correlation_id": "corr-x"})) == 1
        assert len(db.query(_TABLE, {"correlation_id": "corr-y"})) == 1

    def test_handle_materializes_via_injected_db(self) -> None:
        """handle() is the REAL runtime materialization leg.

        The runtime auto-wiring projection callback calls handle(input_data) with
        a DatabaseAdapter at input_data['_db'] and gates row materialization on
        the returned rows_upserted.
        """
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "correlation_id": _CORR,
                "envelope_id": "env-handle",
                "causation_id": _CORR,
                "topic": "onex.cmd.omnimarket.node-generation-requested.v1",  # onex-topic-allow: test fixture literal
                "source_node": "demo_script",
                "timestamp": "2026-06-24T12:00:00Z",
                "payload": {"task_description": "classify"},
                "_db": db,
                "_event_type": "log-entry",
            }
        )
        assert result["rows_upserted"] == 1
        rows = db.query(_TABLE, {"correlation_id": _CORR})
        assert len(rows) == 1
        assert rows[0]["source_node"] == "demo_script"

    def test_chain_reconstructs_deterministically_by_correlation_id(self) -> None:
        """Operator hard-acceptance: given a correlation_id, the ordered event
        chain reconstructs deterministically from canonical projection rows."""
        db = InmemoryDatabaseAdapter()
        chain = [
            (
                "env-0",
                "onex.cmd.omnimarket.node-generation-requested.v1",
                "orchestrator",
            ),  # onex-topic-allow: test fixture
            (
                "env-1",
                "onex.evt.omnimarket.node-generation-completed.v1",
                "consumer",
            ),  # onex-topic-allow: test fixture
            (
                "env-2",
                "onex.evt.omnimarket.node-deployment-completed.v1",
                "deployer",
            ),  # onex-topic-allow: test fixture
        ]
        # Noise from another correlation must not leak into the reconstruction.
        HANDLER.project(_event("noise-0", correlation_id="other"), db)
        for i, (env, topic, node) in enumerate(chain):
            HANDLER.project(
                _event(
                    env,
                    causation_id=chain[i - 1][0] if i else _CORR,
                    topic=topic,
                    source_node=node,
                    timestamp=f"2026-06-24T12:0{i}:00Z",
                    payload={"step": i},
                ),
                db,
            )
        reconstructed = sorted(
            db.query(_TABLE, {"correlation_id": _CORR}), key=lambda r: r["sequence"]
        )
        assert [r["topic"] for r in reconstructed] == [c[1] for c in chain]
        assert [r["source_node"] for r in reconstructed] == [c[2] for c in chain]
        assert [r["sequence"] for r in reconstructed] == [0, 1, 2]
        # Determinism: a repeat reconstruction is byte-identical in order.
        again = sorted(
            db.query(_TABLE, {"correlation_id": _CORR}), key=lambda r: r["sequence"]
        )
        assert [r["envelope_id"] for r in again] == [c[0] for c in chain]


class TestEventChainContractWiring:
    def test_event_bus_subscribes_log_entry(self) -> None:
        with open(_CONTRACT_PATH) as fh:
            contract = yaml.safe_load(fh)
        assert (
            "onex.evt.platform.log-entry.v1"  # onex-topic-allow: contract assertion
            in contract["event_bus"]["subscribe_topics"]
        )

    def test_projection_api_binds_event_chain_table(self) -> None:
        with open(_CONTRACT_PATH) as fh:
            contract = yaml.safe_load(fh)
        assert contract["projection_api"]["table"] == _TABLE
        assert contract["projection_api"]["schema"] == "public"
        assert "correlation_id" in contract["projection_api"]["columns"]

    def test_migration_creates_event_chain_table(self) -> None:
        migration = (
            Path("src/omnimarket/nodes/node_projection_event_chain/migrations")
            / "0001_create_event_chain.sql"
        )
        sql = migration.read_text()
        assert "CREATE TABLE IF NOT EXISTS event_chain" in sql
        assert "correlation_id VARCHAR" in sql
        assert "sequence INTEGER" in sql
