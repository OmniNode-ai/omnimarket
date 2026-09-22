# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the durable prod-promotion-gate decision (OMN-18999).

One hop per line of the node contract's ``golden_path``, so a hop that stops
being true fails here rather than on a lane. The hops that read a contract
read it; the hops that move a decision EXECUTE the real gate and the real
writer.

Hop 4 is the one the ticket is about: a refusal goes in, a row comes out, and
the typed reason, the grant, the requested digest and the evaluation time are
all on it. Before this node existed the same refusal produced nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGateOutcome,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    evaluate_gate,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_prod_promotion_gate_writer import (
    ProdPromotionGateProjectionWriter,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
CONTRACT_PATH = _NODES / "node_projection_prod_promotion_gate/contract.yaml"
PRODUCER_CONTRACT_PATH = _NODES / "node_prod_promotion_gate_compute/contract.yaml"
ORCHESTRATOR_CONTRACT_PATH = _NODES / "node_redeploy_orchestrator/contract.yaml"

GATE_EVALUATE_TOPIC = "onex.cmd.omnimarket.prod-promotion-gate-evaluate.v1"
DECISION_TOPIC = "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-prod-promotion-gate-applied.v1"
SNAPSHOT_TOPIC = "onex.snapshot.projection.prod-promotion-gate.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* prefix by convention
DLQ_TOPIC = "onex.dlq.omnimarket.projection-prod-promotion-gate-malformed.v1"

CORRELATION = UUID("6a2f1d3e-9b47-4c58-8e21-0d5f7a3b9c14")
EVALUATED_AT = datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
BATCH = "promo-batch-2026-09-20"
GRANT_ID = "grant-omn-18999-01"
ROLLBACK = "sha256:" + "3" * 64


def _contract(path: Path) -> dict[str, Any]:
    with open(path) as handle:
        return dict(yaml.safe_load(handle))


def _command(**overrides: Any) -> ModelProdPromotionGateCommand:
    """A prod gate command that would be allowed, with named fields replaced."""
    fields: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "runtime_lane": EnumRuntimeLane.PROD,
        "requested_image_digest": DIGEST,
        "promotion_batch_id": BATCH,
        "readiness_projection": ModelReadinessProjectionFact(
            readiness_state="READY", image_digest=DIGEST, promotion_batch_id=BATCH
        ),
        "occ_gate_state": EnumOccGateState.MERGED,
        "rollback_target": ROLLBACK,
        "requested_by": "jonah",
        "promotion_grant": ModelProdPromotionGrant(
            grant_id=GRANT_ID,
            approved_lane=EnumRuntimeLane.PROD,
            approved_image_digest=DIGEST,
            approved_promotion_batch_id=BATCH,
            approved_by="jonah",
            created_at=EVALUATED_AT - timedelta(hours=1),
            expires_at=EVALUATED_AT + timedelta(hours=1),
        ),
        "evaluated_at": EVALUATED_AT,
    }
    fields.update(overrides)
    return ModelProdPromotionGateCommand(**fields)


class _FakeDb:
    """A database double that remembers what it was asked to store.

    It walks the chain; it does not type-check the SQL. A string bound where a
    TIMESTAMPTZ or a UUID is declared is the question a double cannot answer,
    and it is answered by
    ``test_omn18999_real_postgres_prod_promotion_gate_write_path.py``.
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
                "correlation_id": args[0],
                "outcome": args[1],
                "allowed": args[2],
                "grant_id": args[4],
                "requested_image_digest": args[5],
                "evaluated_at": args[10],
            }
        ]


def test_hop1_the_orchestrator_emits_the_gate_evaluate_command() -> None:
    """Hop 1: the redeploy orchestrator is the producer of the gate command."""
    orchestrator = _contract(ORCHESTRATOR_CONTRACT_PATH)
    assert GATE_EVALUATE_TOPIC in orchestrator["event_bus"]["publish_topics"]

    gate = _contract(PRODUCER_CONTRACT_PATH)
    assert GATE_EVALUATE_TOPIC in gate["event_bus"]["subscribe_topics"]


def test_hop2_the_gate_stamps_a_typed_outcome_and_echoes_the_audit_fields() -> None:
    """Hop 2: the decision carries the four facts the row needs."""
    decision = evaluate_gate(_command(requested_image_digest=OTHER_DIGEST))

    assert decision.allowed is False
    assert decision.outcome is EnumProdGateOutcome.DIGEST_MISMATCH
    assert decision.grant_id == GRANT_ID
    assert decision.requested_image_digest == OTHER_DIGEST
    assert decision.evaluated_at == EVALUATED_AT
    assert decision.correlation_id == CORRELATION


def test_hop3_the_decision_is_published_on_every_outcome() -> None:
    """Hop 3: allowed and refused alike ride the same terminal event."""
    gate = _contract(PRODUCER_CONTRACT_PATH)
    assert gate["terminal_event"] == DECISION_TOPIC
    assert DECISION_TOPIC in gate["event_bus"]["publish_topics"]

    # Executed, not asserted from the contract: both a refusal and an allow
    # produce a decision, so nothing filters one of them off the topic.
    assert evaluate_gate(_command()).allowed is True
    assert evaluate_gate(_command(promotion_grant=None)).allowed is False


def test_hop4_a_refusal_event_becomes_a_row_carrying_the_typed_reason() -> None:
    """Hop 4, the acceptance hop: event in, row out, reason asserted."""
    decision = evaluate_gate(_command(requested_image_digest=OTHER_DIGEST))

    writer = ProdPromotionGateProjectionWriter()
    db = _FakeDb()
    writer._db = db  # type: ignore[assignment]

    payload = decision.model_dump(mode="json")
    payload["_topic"] = DECISION_TOPIC
    payload["_partition"] = 0
    payload["_offset"] = 12
    result = writer.handle(payload)

    assert result["rows_upserted"] == 1
    assert len(db.binds) == 1
    bound = db.binds[0]
    assert bound[0] == CORRELATION
    assert bound[1] == EnumProdGateOutcome.DIGEST_MISMATCH.value
    assert bound[2] is False
    assert bound[4] == GRANT_ID
    assert bound[5] == OTHER_DIGEST
    assert bound[10] == EVALUATED_AT
    assert "ON CONFLICT (correlation_id) DO UPDATE" in db.statements[0]


def test_hop5_the_row_is_published_onto_the_status_page_exposure() -> None:
    """Hop 5: the exposure the always-on status page reads back from."""
    contract = _contract(CONTRACT_PATH)
    assert SNAPSHOT_TOPIC in contract["event_bus"]["publish_topics"]

    exposures = contract["projection_api"]["exposures"]
    assert [exposure["topic"] for exposure in exposures] == [SNAPSHOT_TOPIC]
    assert exposures[0]["bus_backed"] is True
    assert exposures[0]["key_columns"] == ["correlation_id"]

    writer = ProdPromotionGateProjectionWriter()
    resolved = writer._snapshot_exposure
    assert resolved is not None
    assert resolved.topic == SNAPSHOT_TOPIC


def test_hop6_the_projection_applied_event_is_declared_with_a_sink() -> None:
    """Hop 6: the terminal event exists and leaves the graph deliberately."""
    contract = _contract(CONTRACT_PATH)
    assert contract["terminal_event"] == APPLIED_TOPIC
    assert APPLIED_TOPIC in contract["event_bus"]["publish_topics"]
    assert APPLIED_TOPIC in contract["externally_consumed_topics"]
    assert SNAPSHOT_TOPIC in contract["externally_consumed_topics"]


def test_the_malformed_decision_is_quarantined_rather_than_dropped() -> None:
    """The DLQ is declared AND read back from the contract by the writer."""
    contract = _contract(CONTRACT_PATH)
    assert contract["event_bus"]["dlq_topics"] == [DLQ_TOPIC]
    assert ProdPromotionGateProjectionWriter().poison_dlq_topics == [DLQ_TOPIC]


def test_the_golden_path_lines_match_the_hops_asserted_here() -> None:
    """A hop added to the contract with no test here fails the chain."""
    contract = _contract(CONTRACT_PATH)
    assert len(contract["golden_path"]) == 6, (
        "the golden_path gained or lost a hop; add or remove the matching test "
        f"above: {contract['golden_path']}"
    )
