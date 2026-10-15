# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reasoning-case answer oracle (appendix A.4): isolate the final block, compare it."""

import re

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_answer_check_outcome import (
    EnumAnswerCheckOutcome,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_answer_check_request import (
    ModelAnswerCheckRequest,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_answer_check_verdict import (
    ModelAnswerCheckVerdict,
)

_MARKER = re.compile(r"^\s*(?:#+\s*ANSWER|=== ANSWER ===)\s*$", re.IGNORECASE)
_ANSWER_LINE = re.compile(r"^PR-(\d{3}): (BLOCKED|WAIT|NEEDS_REVIEW|MERGE|CONFLICT)$")
_ANSWER_LINES = 12


def _final_block(response: str) -> tuple[list[str], list[str]]:
    """Return the run of answer lines at the very end and the text it was cut from."""
    lines = response.rstrip().split("\n")
    cut = max((i for i, line in enumerate(lines) if _MARKER.match(line)), default=-1)
    lines = [line.rstrip() for line in lines[cut + 1 :]]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines.pop()
    block: list[str] = []
    for line in reversed(lines):
        if not _ANSWER_LINE.match(line):
            break
        block.append(line)
    block.reverse()
    return block, lines


class HandlerDelegationAnswerCheck:
    """Grade one response against the expected lines without executing or reading files."""

    def handle(self, request: ModelAnswerCheckRequest) -> ModelAnswerCheckVerdict:
        expected = [
            line.rstrip() for line in request.expected.split("\n") if line.strip()
        ]
        block, lines = _final_block(request.response)
        ids = [match.group(1) for line in block if (match := _ANSWER_LINE.match(line))]
        failures: list[str] = []
        if not block:
            failures.append(
                f"the response does not end with answer lines; last line is {lines[-1][:60]!r}"
                if lines
                else "empty response"
            )
        elif len(block) != _ANSWER_LINES:
            failures.append(
                f"final block has {len(block)} answer lines, not {_ANSWER_LINES}"
            )
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            failures.append(
                "duplicate ids in the final block: " + ", ".join(duplicates)
            )
        if not failures and block != expected:
            failures.extend(
                f"expected {want!r}, got {got!r}"
                for got, want in zip(block, expected, strict=False)
                if got != want
            )
            if not failures:
                failures.append(
                    f"the expected lines number {len(expected)}, not {_ANSWER_LINES}"
                )
        return ModelAnswerCheckVerdict(
            outcome=EnumAnswerCheckOutcome.FAIL
            if failures
            else EnumAnswerCheckOutcome.PASS,
            run_directory=request.run_directory,
            final_block=tuple(block),
            failures=tuple(failures),
        )
