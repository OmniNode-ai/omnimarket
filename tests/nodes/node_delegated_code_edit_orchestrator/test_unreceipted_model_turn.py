# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A model reply with no `onex delegate` run id is never applied (OMN-20287 AC2).

The loop receipt names every model turn by the `onex delegate` run that served
it. A port that hands back model output with an empty run id would put a model
call in the loop with nothing in the receipt to audit it against, so the loop
treats such a reply as a failed delegate run: its actions never reach the
worktree and the receipt says why.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    ModelTurnReply,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit


def _unreceipted(*, ok: bool = True) -> ModelTurnReply:
    return ModelTurnReply(
        run_id="",
        ok=ok,
        actions=(_a("write", file_path="src/m.py", content=FIX),),
        raw_text="{...}",
    )


def test_a_reply_without_a_run_id_is_not_applied_to_the_worktree() -> None:
    ports = FakePorts([_unreceipted(), _reply(2, _a("finish"))], check_passes=[False])
    request = _request(max_turns=2)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert ports.writes == []
    assert result.delegate_run_ids == ("run-2",)
    receipt = ports.receipts[request.correlation_id]
    first = cast(list[dict[str, Any]], receipt["turns"])[0]
    assert first["ok"] is False
    assert first["actions"] == []
    assert "no onex delegate run id" in first["invalid_reason"]


def test_two_replies_without_a_run_id_end_the_loop_as_delegate_failed() -> None:
    ports = FakePorts([_unreceipted(), _unreceipted()])
    request = _request(max_turns=6)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == EnumCodeEditStatus.DELEGATE_FAILED
    assert ports.writes == []
    assert result.delegate_run_ids == ()
    assert "no onex delegate run id" in result.detail


def test_every_applied_turn_in_the_receipt_names_a_run() -> None:
    ports = FakePorts(
        [
            _reply(1, _a("write", file_path="src/m.py", content=FIX)),
            _reply(2, _a("finish")),
        ],
        check_passes=[True],
    )
    request = _request(max_turns=3)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == EnumCodeEditStatus.ACCEPTED
    turns = cast(list[dict[str, Any]], ports.receipts[request.correlation_id]["turns"])
    assert [t["run_id"] for t in turns] == ["run-1", "run-2"]
    assert all(t["run_id"] for t in turns if t["actions"])
