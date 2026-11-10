# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: appender fails, publisher returns False, naive datetime refused, bad head refused."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    FakePublisher,
)

HEAD = "a" * 40


def _overlay() -> ModelLabProofJudgeUseOverlay:
    return ModelLabProofJudgeUseOverlay(
        schema_version=1,
        event={
            "topic": USE_TOPIC,
            "event_type": "ModelLabProofJudgeUseRecord",
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


def _record(head: str = HEAD) -> ModelLabProofJudgeUseRecord:
    return ModelLabProofJudgeUseRecord(
        repo="example-org",
        pr_number=1,
        head=head,
        class_id="class-1",
        outcome=EnumUseOutcome.not_applicable,
        nodes=("node-a",),
        receipt_ids=("r1",),
        observed_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )


def test_appender_fails_ledger_append_failed_event_still_published() -> None:
    overlay = _overlay()
    appender = FakeAppender(fail=True)
    publisher = FakePublisher()
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=publisher
    )
    result = handler.handle(_record())
    assert result.ledger == "append-failed"
    assert result.event_published is True
    assert len(publisher.events) == 1


def test_publisher_returns_false_event_not_published_row_still_written() -> None:
    overlay = _overlay()
    appender = FakeAppender()
    publisher = FakePublisher(return_false=True)
    handler = HandlerLabProofJudgeUse(
        overlay=overlay, host="host-a", appender=appender, publisher=publisher
    )
    result = handler.handle(_record())
    assert result.event_published is False
    assert result.ledger == "written"
    assert len(appender.rows) == 1


def test_naive_observed_at_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseRecord(
            repo="example-org",
            pr_number=1,
            head=HEAD,
            class_id="class-1",
            outcome=EnumUseOutcome.not_applicable,
            nodes=("node-a",),
            receipt_ids=(),
            observed_at=datetime(2026, 1, 1, 0, 0, 0),  # naive, no tzinfo
        )


def test_head_not_40_hex_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseRecord(
            repo="example-org",
            pr_number=1,
            head="not-a-valid-head",  # not 40 hex chars
            class_id="class-1",
            outcome=EnumUseOutcome.not_applicable,
            nodes=("node-a",),
            receipt_ids=(),
            observed_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
