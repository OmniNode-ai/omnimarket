# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the durable definition-of-done verdict (OMN-18900).

One hop per line of the node contract's ``golden_path``, so a hop that stops
being true fails here rather than on a lane. Hops 1 and 4 read the contract;
hops 2 and 3 execute the product.

Hop 3 is the one the acceptance criterion names: a verdict event goes in, a
row comes out, and the typed refusal for a pass with no behaviour-proving
check is asserted in the same run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    EnumDodEvalOutcome,
    EnumDodEvalRefusal,
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictWire,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
CONTRACT_PATH = _NODES / "node_projection_dod_verdict/contract.yaml"
PRODUCER_CONTRACT_PATH = _NODES / "node_dod_verify/contract.yaml"

VERDICT_TOPIC = "onex.evt.omnimarket.dod-verify-completed.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-dod-verdict-applied.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.projection-dod-verdict-malformed.v1"

CORRELATION = UUID("d4396b48-e783-4523-98b1-5f795b5f7b51")
STARTED = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 20, 11, 4, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    with open(CONTRACT_PATH) as handle:
        return dict(yaml.safe_load(handle))


def _producer_contract() -> dict[str, Any]:
    with open(PRODUCER_CONTRACT_PATH) as handle:
        return dict(yaml.safe_load(handle))


def _event(**overrides: Any) -> dict[str, Any]:
    """A completed verdict as it arrives off the topic."""
    payload: dict[str, Any] = {
        "correlation_id": str(CORRELATION),
        "ticket_id": "OMN-17372",
        "status": "verified",
        "started_at": STARTED.isoformat(),
        "completed_at": COMPLETED.isoformat(),
        "total_checks": 88,
        "verified_count": 16,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        # The shape the 2026-09-20 inventory actually found: almost everything
        # non-probative and nothing behaviour-proving.
        "non_probative_count": 72,
        "behavior_proving_count": 0,
        "readback_proving_count": 0,
        "unbindable_overlay_count": 0,
    }
    payload.update(overrides)
    return payload


class _FakeDb:
    """A database double that remembers what it was asked to store.

    It walks the chain; it does not type-check the SQL. A string bound where
    a TIMESTAMPTZ or a UUID is declared is the question a double cannot
    answer, and it is answered by
    ``test_omn18900_real_postgres_dod_verdict_write_path.py``.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.binds: list[tuple[Any, ...]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append(sql)
        self.binds.append(args)
        return [
            {
                "ticket_id": args[0],
                "correlation_id": args[1],
                "completed_at": args[2],
                "outcome": args[15],
                "outcome_refusal": args[16],
            }
        ]


# --------------------------------------------------------------------------
# Hop 1 — the verdict topic is produced by a node contract, not out of nowhere
# --------------------------------------------------------------------------


def test_hop1_the_subscribed_topic_is_the_verify_node_terminal_event() -> None:
    """This projection subscribes to exactly what node_dod_verify publishes."""
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [VERDICT_TOPIC]

    producer = _producer_contract()
    assert producer["terminal_event"] == VERDICT_TOPIC
    assert VERDICT_TOPIC in producer["event_bus"]["publish_topics"]


def test_hop1_the_producer_no_longer_declares_the_topic_a_sink() -> None:
    """A subscriber makes the sink declaration false, so it had to come out.

    ``externally_consumed_topics`` states that no node contract subscribes to
    a topic. Leaving the entry in place beside this node's subscription is
    precisely the contract-graph drift the closure check exists to catch, and
    the check reads the declarations rather than the intent behind them.
    """
    producer = _producer_contract()
    assert VERDICT_TOPIC not in producer.get("externally_consumed_topics", [])


def test_hop1_no_externally_produced_declaration_is_needed_or_present() -> None:
    """The producer is a node contract, so the external-producer form is wrong.

    Declaring it would assert the topic comes from outside the node graph,
    which is false and would hide a real orphan if the publisher ever went
    away.
    """
    assert "externally_produced_topics" not in _contract()


# --------------------------------------------------------------------------
# Hop 2 — the pure fold, no database in sight
# --------------------------------------------------------------------------


def test_hop2_the_fold_turns_one_verdict_event_into_one_row() -> None:
    """The def-B entry is constructible from the bare event and returns a row.

    The request wraps the event rather than declaring a ``{topic, payload}``
    pair: the runtime adapter builds a def-B input as
    ``input_model_cls(**payload_dict)`` over the unwrapped domain payload, so
    a model shaped as a pair can never be constructed from a bus message at
    all -- a projection can pass a test like this for its whole life while
    folding nothing on the lane.
    """
    event = ModelDodVerdictWire.model_validate(_event())
    result = HandlerProjectionDodVerdict().handle(
        ModelDodVerdictProjectionRequest(event=event)
    )
    assert result.row.ticket_id == "OMN-17372"
    assert result.row.correlation_id == CORRELATION
    assert result.row.completed_at == COMPLETED
    assert result.row.status is EnumDodVerifyStatus.VERIFIED
    assert result.row.total_checks == 88


# --------------------------------------------------------------------------
# Hop 3 — the durable write, and the refusal, in one run
# --------------------------------------------------------------------------


def test_hop3_a_verdict_event_becomes_a_row_and_the_refusal_is_visible() -> None:
    """Project a verdict, assert the row, assert the refusal on zero behaviour.

    The two halves are deliberately in ONE test. A pass with no
    behaviour-proving check is the case the whole second conjunct exists for,
    and asserting the write and the refusal separately would allow a writer
    that stored a row while scoring it done.
    """
    writer = DodVerdictProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    report = writer.handle(_event())

    assert report["rows_upserted"] == 1
    assert len(db.binds) == 1
    bound = db.binds[0]

    # The row: ticket, correlation and completion time are the key.
    assert bound[0] == "OMN-17372"
    assert bound[1] == CORRELATION
    assert bound[2] == COMPLETED
    # The class counts reached it.
    assert bound[6] == 88  # total_checks
    assert bound[11] == 72  # non_probative_count
    assert bound[12] == 0  # behavior_proving_count

    # The refusal: verified, nothing failed, and still not done.
    assert bound[15] == EnumDodEvalOutcome.REFUSED.value
    assert bound[16] == EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK.value
    assert report["dod_verdict_rows"][0]["outcome"] == "refused"
    assert (
        report["dod_verdict_rows"][0]["outcome_refusal"] == "no_behavior_proving_check"
    )


def test_hop3_positive_control_the_same_path_can_reach_done() -> None:
    """One behaviour-proving check flips the same event to done.

    Without this control the refusal above is indistinguishable from a writer
    that refuses everything, which would satisfy the assertion and prove
    nothing.
    """
    writer = DodVerdictProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    report = writer.handle(_event(behavior_proving_count=1, verified_count=17))

    assert report["rows_upserted"] == 1
    assert db.binds[0][15] == EnumDodEvalOutcome.DONE.value
    assert db.binds[0][16] is None


def test_hop3_a_redelivery_converges_on_the_same_key() -> None:
    """The same verdict twice binds the same three key components.

    The upsert converges because the key pins the run; nothing in the write
    path consults a clock for a key column.
    """
    writer = DodVerdictProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    writer.handle(_event())
    writer.handle(_event())

    assert db.binds[0][:3] == db.binds[1][:3]
    assert "ON CONFLICT (ticket_id, correlation_id, completed_at)" in db.statements[0]


def test_hop3_a_re_verification_is_a_new_row_not_an_overwrite() -> None:
    """A later completion time is a different key, which is the history.

    Attempts until the definition of done verifies true is a question about a
    ticket's history. A key that collapsed re-verifications onto one row
    could never answer it.
    """
    writer = DodVerdictProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    later = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)
    writer.handle(_event())
    writer.handle(_event(completed_at=later.isoformat(), behavior_proving_count=1))

    assert db.binds[0][2] == COMPLETED
    assert db.binds[1][2] == later
    assert db.binds[0][15] != db.binds[1][15]


# --------------------------------------------------------------------------
# Hop 4 — the applied event and the quarantine path are declared
# --------------------------------------------------------------------------


def test_hop4_the_terminal_event_and_dlq_are_declared() -> None:
    contract = _contract()
    assert contract["terminal_event"] == APPLIED_TOPIC
    assert contract["event_bus"]["publish_topics"] == [APPLIED_TOPIC]
    assert contract["event_bus"]["dlq_topics"] == [DLQ_TOPIC]
    assert contract["externally_consumed_topics"] == [APPLIED_TOPIC]


def test_hop4_the_contract_declares_the_table_it_writes() -> None:
    """Without the db_io block the consumer advances offsets and never writes."""
    tables = _contract()["db_io"]["db_tables"]
    assert len(tables) == 1
    table = tables[0]
    assert table["name"] == "dod_verify_runs"
    assert table["schema"] == "omninode_internal"
    assert table["migration"] == "0000_create_dod_verify_runs.sql"
    assert table["access"] == "write"


def test_hop4_the_dedupe_key_matches_the_upsert_conflict_target() -> None:
    """The declared idempotency and the SQL that enforces it cannot diverge."""
    from omnimarket.nodes.node_projection_dod_verdict.handlers import (
        handler_dod_verdict_runner,
    )

    declared = _contract()["db_io"]["dedupe_key"]
    assert declared == ["ticket_id", "correlation_id", "completed_at"]
    conflict = "ON CONFLICT (" + ", ".join(declared) + ")"
    assert conflict in handler_dod_verdict_runner._UPSERT


def test_hop4_only_the_writer_is_routed() -> None:
    """The writer is the one routed operation; the pure fold is not (OMN-18901).

    A routing entry with no event_model is dispatched on every subscribe
    topic. Routed, the fold ran beside the writer on each verdict and failed
    the dispatch, so every stored verdict was also dead-lettered, replayed and
    quarantined on the .201 dev lane. The fold stays the contract's declared
    def-B handler and runs inside the writer.
    """
    contract = _contract()
    routed = {
        entry["operation"]: entry["handler"]["name"]
        for entry in contract["handler_routing"]["handlers"]
    }
    assert routed == {"dod_verdict_projection_writer": "DodVerdictProjectionWriter"}
    assert contract["handler"]["class"] == "HandlerProjectionDodVerdict"
    assert DodVerdictProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.asyncio
async def test_hop3_the_standalone_entry_writes_the_same_row() -> None:
    """``project_event`` is the other caller, and it must not diverge.

    The runtime dispatches ``handle`` in-process; the base class's own
    consume loop calls ``project_event`` and commits offsets on its boolean.
    Both funnel into one private method here, and this test is what keeps
    that true -- a second write path is how one of them silently stops
    writing.

    The four tests above call ``handle`` synchronously on purpose: it opens
    its own event loop per message, which is the promise the in-process
    dispatch declaration makes, and ``asyncio.run`` cannot be re-entered from
    inside a running loop.
    """
    writer = DodVerdictProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    ok = await writer.project_event(
        VERDICT_TOPIC, _event(), MessageMeta(partition=0, offset=1, fallback_id="gc")
    )

    assert ok is True
    assert len(db.binds) == 1
    assert db.binds[0][0] == "OMN-17372"
    assert db.binds[0][15] == EnumDodEvalOutcome.REFUSED.value
