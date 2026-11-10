# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden: a not_applicable use publishes one event and appends one row; idempotent repeat."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_lab_proof_judge_use_effect.handlers.handler_lab_proof_judge_use import (
    HandlerLabProofJudgeUse,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.models.enum_use_outcome import (
    EnumUseOutcome,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.models.model_lab_proof_judge_use_record import (
    ModelLabProofJudgeUseRecord,
)
from tests.unit.nodes.node_lab_proof_judge_use_effect.conftest import USE_TOPIC

HEAD = "a" * 40


def _record(
    outcome: EnumUseOutcome = EnumUseOutcome.not_applicable,
) -> ModelLabProofJudgeUseRecord:
    return ModelLabProofJudgeUseRecord(
        repo="example-org",
        pr_number=42,
        head=HEAD,
        class_id="class-abc",
        outcome=outcome,
        nodes=("node-a", "node-b"),
        receipt_ids=("receipt-1", "receipt-2"),
        observed_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


def test_not_applicable_publishes_one_event_and_one_row(
    overlay, appender, publisher
) -> None:
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=publisher
    )
    result = handler.handle(_record())

    # exactly one event on the declared topic
    assert len(publisher.events) == 1
    assert publisher.events[0]["topic"] == USE_TOPIC
    assert publisher.events[0]["event_type"] == "ModelLabProofJudgeUseRecord"
    assert result.event_published is True
    assert result.event_id == publisher.events[0]["event_id"]

    # exactly one row
    assert len(appender.rows) == 1
    row = appender.rows[0]
    assert "example-org#42" in row
    assert HEAD in row
    assert "class-abc" in row
    assert "receipt-1,receipt-2" in row
    assert "not_applicable" in row
    assert result.ledger == "written"
    assert result.row + "\n" == row


def test_repeat_yields_same_event_id_and_already_written(
    overlay, appender, publisher
) -> None:
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=publisher
    )
    record = _record()
    result1 = handler.handle(record)
    # simulate the ledger already having this row
    appender.already_written = True
    result2 = handler.handle(record)
    assert result1.event_id == result2.event_id
    assert result2.ledger == "already-written"
    assert result2.event_published is True


def test_refused_no_substitute_records_outcome_and_none_substitute(
    overlay, appender, publisher
) -> None:
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=publisher
    )
    record = ModelLabProofJudgeUseRecord(
        repo="example-org",
        pr_number=7,
        head=HEAD,
        class_id="class-xyz",
        outcome=EnumUseOutcome.refused_no_substitute,
        nodes=("node-c",),
        receipt_ids=(),
        observed_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    result = handler.handle(record)
    assert result.event_published is True
    assert "refused_no_substitute" in result.row
    assert "substitute=none" in result.row
    assert result.ledger == "written"
