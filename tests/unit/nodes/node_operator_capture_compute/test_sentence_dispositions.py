# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each sentence keeps a source-linked disposition, including chat and replay."""

from datetime import UTC, datetime

import pytest

from omnimarket.models.operator_capture import (
    ModelCaptureRowsRequest,
    ModelUtterance,
    ModelUtteranceClassifyRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_capture_rows import (
    HandlerCaptureRows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    HandlerUtteranceClassify,
)

pytestmark = pytest.mark.unit


def test_disposition_sentence_numbers_do_not_split_decimal_versions() -> None:
    utterance = ModelUtterance(
        capture_id="decimal",
        text="Check M4.5 today. Thanks.",
        session_id="session-a",
        source="operator",
        captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    classified = HandlerUtteranceClassify().handle(
        ModelUtteranceClassifyRequest(utterance=utterance)
    )
    assert [d.quote for d in classified.dispositions] == [
        "Check M4.5 today.",
        "Thanks.",
    ]


def test_mixed_prompt_dispositions_and_replay() -> None:
    utterance = ModelUtterance(
        capture_id="mixed",
        text="Build the reducer. Thanks. Check the result. Good night.",
        session_id="session-a",
        source="operator",
        captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    classified = HandlerUtteranceClassify().handle(
        ModelUtteranceClassifyRequest(utterance=utterance)
    )
    request = ModelCaptureRowsRequest(
        utterance=utterance, classification=classified, stamp=utterance.captured_at
    )
    rows = HandlerCaptureRows().handle(request).rows
    dispositions = [r for r in rows if " | kind=disposition | " in r]
    assert len(dispositions) == 4
    assert all(" | capture=mixed | " in row for row in dispositions)
    assert [f"sentence={i}" in dispositions[i - 1] for i in range(1, 5)] == [True] * 4
    assert sum(" | item=chat | " in r for r in dispositions) == 2
    items = [
        r.split(" | item=", 1)[1].split(" | ", 1)[0]
        for r in dispositions
        if " | item=chat | " not in r
    ]
    assert len(set(items)) == 2
    assert (
        HandlerCaptureRows()
        .handle(request.model_copy(update={"existing_rows": rows}))
        .rows
        == ()
    )


def test_delegated_partial_coverage_keeps_every_sentence() -> None:
    utterance = ModelUtterance(
        capture_id="partial",
        text="Build the reducer. Thanks. Check the result.",
        session_id="session-a",
        source="operator",
        captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    classified = HandlerUtteranceClassify().handle(
        ModelUtteranceClassifyRequest(
            utterance=utterance,
            delegated_response='{"items": [{"kind":"ask", "quote":"Build the reducer.", "subject":"reducer", "confidence":1.0}]}',
            delegated_model="second-engine",
        )
    )
    result = HandlerCaptureRows().handle(
        ModelCaptureRowsRequest(
            utterance=utterance, classification=classified, stamp=utterance.captured_at
        )
    )
    assert len([r for r in result.rows if " | kind=disposition | " in r]) == 3
    assert len([r for r in result.rows if " | kind=ask | " in r]) == 2
