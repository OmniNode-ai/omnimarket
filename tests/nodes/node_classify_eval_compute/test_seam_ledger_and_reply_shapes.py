# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams: the ledger line format the build step reads, and the reply shapes the delegation step returns.

Both fixtures keep the shape of real runs (the cell order of an integration ledger line, with the long
evidence cell; the reply forms read from delegation receipts and GLM harness answers) with synthetic wording,
because this repository is public. A change to either side's shape fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import ModelClassifyEvalRequest
from omnimarket.nodes.node_classify_eval_compute.handlers.handler_classify_eval import (
    HandlerClassifyEval,
    parse_class,
)
from tests.nodes.node_classify_eval_compute.support import overlay

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
C = EnumIntegrationClass


def test_ledger_lines_in_the_real_shape_parse_into_labelled_tasks() -> None:
    text = (FIXTURES / "ledger_sample.md").read_text(encoding="utf-8")
    handler = HandlerClassifyEval(
        overlay=overlay(per_class_n=dict.fromkeys(C, 1), min_total=1)
    )
    result = handler.handle(
        ModelClassifyEvalRequest(
            operation=EnumClassifyOperation.BUILD_SET, suite_id="s", ledger_text=text
        )
    )
    assert result.failure is None, result.detail
    assert {t.label for t in result.tasks} == set(C)
    for t in result.tasks:
        assert t.title
        assert t.merged_at
        assert t.evidence
        assert "state:" not in t.evidence
        assert "owner:" not in t.evidence


def test_reply_forms_parse_to_a_class() -> None:
    rows = json.loads((FIXTURES / "real_replies.json").read_text(encoding="utf-8"))
    assert len(rows) == 5
    for row in rows:
        assert parse_class(row["reply"]) is not None, row["source"]
    assert [parse_class(row["reply"]) for row in rows] == [
        C.NOT_RUNNING,
        C.SEEN_RUNNING,
        C.RUN_NOT_SEEN,
        C.BLOCKED,
        C.NOT_RUNNING,
    ]
