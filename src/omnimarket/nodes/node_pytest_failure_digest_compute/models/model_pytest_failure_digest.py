# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the pytest failure digest compute (OMN-19360).

The input is what one focused run produced: pytest's own junit XML and the
process exit code. The output is small enough to hand back to a local model
as repair context, and carries a fingerprint so a loop can tell "the same
failure again" from "a different failure": two runs that differ only in
timing, host name or temp paths get the same fingerprint.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_MESSAGE_CHARS = 500
MAX_FRAMES_CHARS = 1500


class EnumPytestRunOutcome(StrEnum):
    """What one run means. Only ``passed`` is a pass."""

    PASSED = "passed"
    FAILED_CALL = "failed_call"
    FAILED_COLLECTION = "failed_collection"
    ERROR_SETUP = "error_setup"
    NO_TESTS = "no_tests"
    INFRA_ERROR = "infra_error"


class ModelPytestRunReport(BaseModel):
    """One run's raw evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    junit_xml: str = Field(default="", description="pytest --junitxml output.")
    exit_code: int | None = Field(
        default=None, description="pytest's exit code; None when it never ran."
    )


class ModelPytestFailureDigest(BaseModel):
    """A bounded account of one run, fit to feed back to a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumPytestRunOutcome
    tests: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failing_node_id: str = ""
    exception_type: str = ""
    message: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    frames: str = Field(default="", max_length=MAX_FRAMES_CHARS)
    top_frame: str = Field(default="", description="file:line of the crash frame.")
    fingerprint: str = Field(
        default="",
        description="sha256 over outcome, failing node id, exception type and "
        "top frame; empty for a pass.",
    )


__all__ = [
    "MAX_FRAMES_CHARS",
    "MAX_MESSAGE_CHARS",
    "EnumPytestRunOutcome",
    "ModelPytestFailureDigest",
    "ModelPytestRunReport",
]
