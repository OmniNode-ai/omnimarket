# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_question_guard_compute: request in, typed verdict out.

Drives the node entry point (NodeQuestionGuardCompute -> HandlerQuestionGuard)
with the contract's own rule data and asserts the full result chain: a refused
question names its ruling and rule, an admitted question carries none.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_question_guard_compute import NodeQuestionGuardCompute
from omnimarket.nodes.node_question_guard_compute.models import (
    EnumQuestionGuardVerdict,
    ModelQuestionGuardRequest,
)

pytestmark = pytest.mark.unit

RESTORE = "2026-10-20T17:35:21Z"
ROWS = (f"{RESTORE} | RULING | lane=orchestrator | ticket=OMN-17427 | text=scrubbed",)


def test_golden_chain_node_question_guard_compute_refuses_a_restore_question() -> None:
    request = ModelQuestionGuardRequest(
        reply_text="The dev lane is down. Should I restore the dev lane now?",
        ruling_rows=ROWS,
    )
    result = NodeQuestionGuardCompute().handle(request)
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.ruling_id == RESTORE
    assert result.rule_id == "dev-lane-restore"
    assert result.questions


def test_golden_chain_node_question_guard_compute_admits_a_statement() -> None:
    request = ModelQuestionGuardRequest(
        reply_text="I restored the dev lane at 14:02Z; it is healthy.",
        ruling_rows=ROWS,
    )
    result = NodeQuestionGuardCompute().handle(request)
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT
    assert result.ruling_id == ""
    assert result.questions == ()
