# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline blind grader for the SWE-discriminator smoke run (OMN-13988).

VERIFIER != RUNNER: this module runs in a separate pass over captured
artifacts. It sees only the task spec + the produced artifact (arm identity is
stripped before grading). The deterministic HARD FLOOR reuses the graded_ladder
isolated-subprocess executor: assemble the task's grader_preamble + the arm's
extracted code + the held-back asserts, exec it with a minimal environment and
a wall-clock timeout, and return a hard pass/fail. No LLM-judge in the smoke —
the floor alone answers the go/no-go question (usable rows produced?).
"""

from __future__ import annotations

import re

from omnimarket.delegation.graded_ladder.graders import (
    extract_code_block,
    run_code_asserts,
)
from omnimarket.delegation.swe_discriminator.models import SweTask


def missing_defs(code: str, required: list[str]) -> list[str]:
    return [
        name
        for name in required
        if not re.search(rf"\b(?:def|class)\s+{re.escape(name)}\b", code)
    ]


def grade_floor(task: SweTask, artifact: str) -> tuple[bool, str]:
    """Deterministic hard floor. Returns (passed, detail).

    A truncated / empty artifact fails as a plumbing failure (distinguished in
    the detail) rather than being scored as a capability failure.
    """

    code = extract_code_block(artifact)
    if not code.strip():
        return False, "no code recovered from artifact (empty/truncated)"

    missing = missing_defs(code, task.required_defs)
    if missing:
        return False, f"artifact missing required defs: {missing}"

    candidate = f"{task.grader_preamble}\n{code}"
    passed, detail = run_code_asserts(
        candidate,
        entrypoint=None,
        asserts=task.held_back_asserts,
        timeout_s=20.0,
    )
    return passed, detail
