# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome classification for the SWE-discriminator harness (OMN-13988).

The load-bearing distinction (the OMN-13335 hazard): a token-cap truncation that
leaves no usable code is a PLUMBING failure, not a capability failure. If it were
scored as FAIL it would deflate the hard-tier (L3/L4) capability numbers and the
whole experiment would lie. ``detect_truncation`` and ``classify_run`` separate:

* PASS         — floor passed (real signal)
* FAIL_WRONG   — complete, non-truncated artifact that failed the floor (real)
* TRUNCATED    — a producing call hit the token cap AND left no usable code
                 (excluded from scoring; a real run would re-run with a bigger
                 budget)
* BLOCKED      — infra (rate-limit / unreachable) (excluded)
* NO_ARTIFACT  — empty output, neither truncated nor blocked
"""

from __future__ import annotations

import ast

from omnimarket.delegation.graded_ladder.graders import extract_code_block
from omnimarket.delegation.swe_discriminator.grader import missing_defs
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumRunOutcome,
    SweTask,
)

# Roles whose truncation actually loses the answer. A truncated *decomposer* just
# yields a degraded slice plan (handled elsewhere); a truncated *producing* call
# (monolith / slice worker) is what strands the code.
_PRODUCING_ROLES = frozenset({"monolith", "worker"})


def _hit_token_cap(run: ArmRun) -> bool:
    return any(
        c.finish_reason == "length"
        for c in run.calls
        if c.role in _PRODUCING_ROLES and not c.error
    )


def has_usable_code(task: SweTask, artifact: str) -> bool:
    """True when the artifact yields extractable, PARSEABLE code defining the
    required names.

    Parseability matters: a token-cap truncation can leave a block that has the
    required ``def`` header but is cut off mid-body (an unterminated string /
    dangling block), which is not usable code even though the def name is
    present.
    """

    code = extract_code_block(artifact)
    if not code.strip():
        return False
    if missing_defs(code, task.required_defs):
        return False
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return True


def detect_truncation(task: SweTask, run: ArmRun) -> bool:
    """A producing call hit the token cap AND the artifact has no usable code.

    Both conditions are required: a run that hit the cap but still emitted a
    complete, parseable, gradeable code block is NOT truncated-lossy — score it
    normally. A cap hit whose code is empty, missing the required def, or
    unparseable (cut mid-body) is a lossy truncation → excluded.
    """

    return _hit_token_cap(run) and not has_usable_code(task, run.artifact)


def classify_run(task: SweTask, run: ArmRun, *, floor_passed: bool) -> EnumRunOutcome:
    """Classify one run into a capability signal or an excluded plumbing/infra event."""

    if run.blocked:
        return EnumRunOutcome.BLOCKED
    if detect_truncation(task, run):
        return EnumRunOutcome.TRUNCATED
    if not run.artifact.strip():
        return EnumRunOutcome.NO_ARTIFACT
    return EnumRunOutcome.PASS if floor_passed else EnumRunOutcome.FAIL_WRONG
