# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.enum_tool_outcome import (
    EnumToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelHookToolOutcomeRequest,
    ModelToolOutcomeRules,
)

EXAMPLE = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_tool_outcome_compute"
    / "overlay.example.yaml"
)


def _rules() -> ModelToolOutcomeRules:
    return ModelToolOutcomeRules.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def _handler() -> HandlerHookToolOutcome:
    return HandlerHookToolOutcome(_rules())


GOLDEN_CASES: list[
    tuple[
        str, ModelHookToolOutcomeRequest, tuple[EnumToolOutcome, str | None, str | None]
    ]
] = [
    (
        "bash_refused_component_line",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "stdout": "REFUSED factory-floor-publish: the required step failed",
                    "stderr": "",
                    "interrupted": False,
                }
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.REFUSED, "factory-floor-publish", "refused_component_line"),
    ),
    (
        "bash_refused_args",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "stdout": "REFUSED 64 args: a TERMINAL row needs x",
                    "stderr": "",
                    "interrupted": False,
                }
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.REFUSED, "args", "refused_component_line"),
    ),
    (
        "bash_refused_ticket_held",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "stdout": "REFUSED OMN-23963 is held by lane abc",
                    "stderr": "",
                    "interrupted": False,
                }
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.REFUSED, "ticket-hold", "refused_ticket_held"),
    ),
    (
        "bash_refused_no_consumer",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "stdout": '{"status": "no_consumer_for_topic"}',
                    "stderr": "",
                    "interrupted": False,
                }
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.REFUSED, "no_consumer_for_topic", "status_refused_json"),
    ),
    (
        "bash_failed",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUseFailure",
            error="Exit code 1\nls: cannot access x",
            exit_code=1,
        ),
        (EnumToolOutcome.FAILED, None, None),
    ),
    (
        "bash_timeout_exit_code",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUseFailure",
            error="Exit code 124\n",
            exit_code=124,
        ),
        (EnumToolOutcome.TIMEOUT, None, "timeout"),
    ),
    (
        "bash_interrupted_timeout",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps({"stdout": "", "stderr": "", "interrupted": True}),
            exit_code=0,
        ),
        (EnumToolOutcome.TIMEOUT, None, "timeout"),
    ),
    (
        "plain_success",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {"stdout": "hello", "stderr": "", "interrupted": False}
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.OK, None, None),
    ),
    (
        "read_tool_refused_line_is_ok",
        ModelHookToolOutcomeRequest(
            tool_name="Read",
            hook_event_name="PostToolUse",
            tool_response="REFUSED factory-floor-publish: x",
            exit_code=0,
        ),
        (EnumToolOutcome.OK, None, None),
    ),
    (
        "bash_code_text_refused_too_long_is_ok",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {"stdout": "REFUSED_TOO_LONG = 1", "stderr": "", "interrupted": False}
            ),
            exit_code=0,
        ),
        (EnumToolOutcome.OK, None, None),
    ),
    (
        "bash_stderr_timeout_marker",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "stdout": "",
                    "stderr": "Command timed out after 120s",
                    "interrupted": False,
                }
            ),
        ),
        (EnumToolOutcome.TIMEOUT, None, "timeout"),
    ),
    (
        "stored_shape_refusal_below_other_output",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response=json.dumps(
                {
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                    "stderr": "",
                    "stdout": "reading rows\nREFUSED 5 live-read: the host did not answer\n",
                }
            ),
        ),
        (EnumToolOutcome.REFUSED, "live-read", "refused_component_line"),
    ),
    (
        "failure_text_names_a_held_ticket",
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUseFailure",
            error="Exit code 1\nREFUSED OMN-23963 is held by lane abc",
        ),
        (EnumToolOutcome.REFUSED, "ticket-hold", "refused_ticket_held"),
    ),
]


@pytest.mark.parametrize(
    ("case_name", "req", "expected"),
    GOLDEN_CASES,
    ids=[c[0] for c in GOLDEN_CASES],
)
def test_golden(
    case_name: str,
    req: ModelHookToolOutcomeRequest,
    expected: tuple[EnumToolOutcome, str | None, str | None],
) -> None:
    handler = _handler()
    result = handler.handle(req)
    assert result.outcome == expected[0]
    assert result.refusing_component == expected[1]
    assert result.matched_rule == expected[2]


def test_determinism() -> None:
    handler = _handler()
    request = ModelHookToolOutcomeRequest(
        tool_name="Bash",
        hook_event_name="PostToolUse",
        tool_response=json.dumps(
            {
                "stdout": "REFUSED factory-floor-publish: x",
                "stderr": "",
                "interrupted": False,
            }
        ),
        exit_code=0,
    )
    r1 = handler.handle(request)
    r2 = handler.handle(request)
    assert r1 == r2


def test_refusal_beats_exit_code_zero() -> None:
    handler = _handler()
    request = ModelHookToolOutcomeRequest(
        tool_name="Bash",
        hook_event_name="PostToolUse",
        tool_response=json.dumps(
            {
                "stdout": "REFUSED factory-floor-publish: x",
                "stderr": "",
                "interrupted": False,
            }
        ),
        exit_code=0,
    )
    result = handler.handle(request)
    assert result.outcome == EnumToolOutcome.REFUSED
