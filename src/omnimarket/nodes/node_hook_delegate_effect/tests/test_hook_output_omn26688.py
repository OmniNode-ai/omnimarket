# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An answered subagent becomes a PreToolUse deny whose reason is the result (OMN-26688).

Failure modes: the deny is not one line, so the hook cannot take it as its last line;
the reason carries runner markers or telemetry; a multi-line or JSON answer is cut;
a result that was not answered still denies the subagent.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_hook_delegate_effect.models.model_hook_delegate import (
    EnumHookDelegateOutcome,
    ModelHookDelegateResult,
)


def test_hook_output_answered_structure_and_reason() -> None:
    """Answered result yields one-line JSON deny with exact reason and no legacy markers."""
    answer = (
        "A Python dictionary is a mutable collection of key-value pairs.\n\n"
        "Keys are unique and hashable.\n"
    )
    result = ModelHookDelegateResult(
        outcome=EnumHookDelegateOutcome.ANSWERED,
        answer=answer,
        model="Qwen3.8-27B",
        run_id="3b917cd9",
    )
    hook_output = result.hook_output
    assert "\n" not in hook_output
    payload = json.loads(hook_output)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    expected_answer = answer.rstrip()
    expected_reason = (
        "Subagent task completed by onex delegation with Qwen3.8-27B "
        "(run 3b917cd9), in place of a Claude subagent. "
        "This is the subagent's result: relay it as the answer and do not "
        "launch the subagent again.\n\nResult:\n" + expected_answer
    )
    assert specific["permissionDecisionReason"] == expected_reason
    for marker in ("DELEGATED:", "DELEGATION RESULT", "delegated_to"):
        assert marker not in specific["permissionDecisionReason"]


@pytest.mark.parametrize(
    "answer",
    [
        '{"name": "dict", "mutable": true}',
        "line one\nline two\nline three",
    ],
)
def test_hook_output_reason_ends_with_result_and_answer(answer: str) -> None:
    """Reason ends with 'Result:\n' plus the answer with trailing whitespace removed."""
    result = ModelHookDelegateResult(
        outcome=EnumHookDelegateOutcome.ANSWERED,
        answer=answer,
        model="m",
        run_id="r",
    )
    payload = json.loads(result.hook_output)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.endswith("Result:\n" + answer.rstrip())


@pytest.mark.parametrize(
    "outcome",
    [
        EnumHookDelegateOutcome.NO_ANSWER,
        EnumHookDelegateOutcome.FAILED,
        EnumHookDelegateOutcome.DECLINED,
    ],
)
def test_hook_output_empty_when_not_answered(outcome: EnumHookDelegateOutcome) -> None:
    """Non-answered outcomes return an empty hook_output."""
    result = ModelHookDelegateResult(
        outcome=outcome,
        answer="some answer",
        model="m",
        run_id="r",
    )
    assert result.hook_output == ""
