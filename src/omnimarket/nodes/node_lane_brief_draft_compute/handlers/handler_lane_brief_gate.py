# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge a drafted brief by the brief checker's own result and name the next move.

The checker (``lane_brief.py check``) is the only acceptance authority: exit 0 is the only
dispatchable result. A refusal is split by who can fix it. A problem in a shell part (the 3a block,
the standing rules, a ticket-state acknowledgement or a quoted criterion) is code's to regenerate and
the model is never asked to repair it; a free-text problem sends the draft back to the local rung while
attempts remain, and then to a Claude-written brief. No move names another model rung.
"""

from __future__ import annotations

from omnimarket.models.lane_brief_draft.enum_brief_gate_verdict import (
    EnumBriefGateVerdict,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_gate_request import (
    ModelLaneBriefGateRequest,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_gate_result import (
    ModelLaneBriefGateResult,
)

_EXIT_PASS = 0
_EXIT_REFUSED = 3

# What the checker prints for a part that code fills. Matched as substrings of one problem line.
SHELL_PROBLEM_MARKERS = (
    "3a.",
    "regenerate 3a",
    "standing rules block",
    "ticket-state-ack",
    "ticket-criteria",
    "differs from the live ticket's criterion lines",
)


def _is_shell_problem(problem: str) -> bool:
    return any(marker in problem for marker in SHELL_PROBLEM_MARKERS)


class HandlerLaneBriefGate:
    """Pure: reads no file, runs no checker and calls no model."""

    def handle(self, request: ModelLaneBriefGateRequest) -> ModelLaneBriefGateResult:
        code = request.checker_exit_code
        problems = request.checker_problems
        if code == _EXIT_PASS and not problems:
            return ModelLaneBriefGateResult(
                verdict=EnumBriefGateVerdict.ACCEPTED, dispatchable=True
            )
        if code != _EXIT_REFUSED or not problems:
            return ModelLaneBriefGateResult(
                verdict=EnumBriefGateVerdict.UNREADABLE, dispatchable=False
            )
        shell = tuple(p for p in problems if _is_shell_problem(p))
        free_text = tuple(p for p in problems if not _is_shell_problem(p))
        if shell and request.shell_regenerations < 1:
            verdict = EnumBriefGateVerdict.REGENERATE_SHELL
        elif shell or request.attempt >= request.max_attempts:
            verdict = EnumBriefGateVerdict.WRITE_WITH_CLAUDE
        else:
            verdict = EnumBriefGateVerdict.REDRAFT_LOCAL
        return ModelLaneBriefGateResult(
            verdict=verdict,
            dispatchable=False,
            shell_problems=shell,
            free_text_problems=free_text,
            defect_suspected=bool(shell) and request.shell_regenerations >= 1,
        )
