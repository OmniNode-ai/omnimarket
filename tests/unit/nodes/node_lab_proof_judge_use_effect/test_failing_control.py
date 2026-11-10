# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failing control: publisher=None means event_published is False and the recorder is empty."""

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
from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    ModelLabProofJudgeUseOverlay,
)
from tests.unit.nodes.node_lab_proof_judge_use_effect.conftest import (
    USE_TOPIC,
    FakeAppender,
)

HEAD = "c" * 40


def _overlay() -> ModelLabProofJudgeUseOverlay:
    return ModelLabProofJudgeUseOverlay(
        schema_version=1,
        event={
            "topic": USE_TOPIC,
            "event_type": "lab_proof_judge.use_recorded",
        },
        ledger={
            "append": {
                "command_env": "EXAMPLE_LEDGER_APPEND_COMMAND",
                "host_env": "EXAMPLE_HOST_NAME",
                "timeout_seconds": 60,
            },
            "lane": "lab-proof-judge",
            "actor": "node:node_lab_proof_judge_use_effect",
            "ticket": "OMN-23642",
            "kind_cell": "lab-proof-class-use",
        },
    )


def test_publisher_none_event_not_published_recorder_empty() -> None:
    overlay = _overlay()
    appender = FakeAppender()
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=None
    )
    record = ModelLabProofJudgeUseRecord(
        repo="example-org",
        pr_number=1,
        head=HEAD,
        class_id="class-1",
        outcome=EnumUseOutcome.not_applicable,
        nodes=("node-a",),
        receipt_ids=("r1",),
        observed_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    result = handler.handle(record)
    assert result.event_published is False
    # the golden assertion "one event on the declared topic" would fail: no publisher, no events
    assert appender.rows  # the row was still written
    assert result.ledger == "written"
