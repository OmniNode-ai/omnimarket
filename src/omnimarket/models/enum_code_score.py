# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed enums of the code contract score (OMN-23626)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCodeScoreVerdict(StrEnum):
    """The ``checks`` level of a score. ``PASS`` means every bound check ran and exited 0.

    Neither a check that hit its wall-clock limit nor an input refused for its size is a verdict on the draft: a limit
    is ``unscorable`` with source ``harness``, and the input-size ceiling is enforced upstream of this step (OMN-23635).
    """

    PASS = "pass"
    FAIL_CHECK = "fail_check"
    FAIL_NO_DIFF = "fail_no_diff"
    FAIL_DID_NOT_APPLY = "fail_did_not_apply"
    FAIL_TAMPERED = "fail_tampered"
    UNSCORABLE = "unscorable"

    @property
    def is_fail(self) -> bool:
        """True for the verdicts that count as a failure of the draft."""
        return self.value.startswith("fail_")


@unique
class EnumCodeScoreSource(StrEnum):
    """Why a draft could not be scored: an infrastructure fault, never the draft's own."""

    CONTRACT = "contract"
    CHECK_NOT_RUNNABLE = "check_not_runnable"
    FALSIFIER_NOT_HELD = "falsifier_not_held"
    ENV = "env"
    WORKTREE = "worktree"
    HARNESS = "harness"
    CONTEXT = "context"
    DRAFT_NOT_RECOVERABLE = "draft_not_recoverable"


@unique
class EnumCodeScoreRuntime(StrEnum):
    """The ``runtime`` level: never a pass in the eval, where a draft is not deployed."""

    UNREAD_EVAL = "unread(eval)"
    UNREAD = "unread"


@unique
class EnumCodeScoreCheckOutcome(StrEnum):
    """What one bound check did in the scratch worktree."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_RUNNABLE = "not_runnable"
    NOT_RUN = "not_run"


@unique
class EnumCodeScoreFailureCode(StrEnum):
    """Why a request was refused before any verdict (the failed topic's typed code)."""

    CONTRACT_SHA256_MISMATCH = "contract_sha256_mismatch"
    CHECK_FILE_SHA256_MISMATCH = "check_file_sha256_mismatch"
    CHECK_FILE_UNREADABLE = "check_file_unreadable"
    REQUEST_INVALID = "request_invalid"
    INTERNAL_ERROR = "internal_error"


__all__ = [
    "EnumCodeScoreCheckOutcome",
    "EnumCodeScoreFailureCode",
    "EnumCodeScoreRuntime",
    "EnumCodeScoreSource",
    "EnumCodeScoreVerdict",
]
