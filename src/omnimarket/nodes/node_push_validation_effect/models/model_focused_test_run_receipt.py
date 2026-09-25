# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelFocusedTestRunReceipt — the outcome of one focused container run
(OMN-19359).

A receipt says what RAN, not what it means. Whether the test passed, failed in
the call phase or failed at collection is read from ``junit_xml`` by the
failure-digest compute (task T4), never decided here. This model decides only
whether the run is usable evidence at all:

* ``completed`` — the container ran, a junit file came back, and both
  teardown checks passed.
* ``host_busy`` — admission refused the run before anything was created.
* ``infra_error`` — anything else: an ssh or git failure, a mutation that did
  not match exactly once, a missing junit file, or a teardown check that did
  not pass. An infrastructure fault is never a pass.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Cap on the junit XML carried in the receipt.
MAX_JUNIT_BYTES: int = 262_144


class EnumFocusedTestRunStatus(StrEnum):
    COMPLETED = "completed"
    HOST_BUSY = "host_busy"
    INFRA_ERROR = "infra_error"


class ModelFocusedTestRunReceipt(BaseModel):
    """What one focused run did, and whether it cleaned up after itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    ref_role: str
    attempt: int
    commit_sha: str
    test_node_id: str
    status: EnumFocusedTestRunStatus
    exit_code: int | None = Field(
        default=None, description="Container exit code; None when it never ran."
    )
    junit_xml: str = Field(
        default="",
        description="pytest's junit XML, capped; empty when none came back.",
    )
    junit_truncated: bool = False
    image_id: str = Field(default="", description="The environment image id.")
    host: str = Field(default="", description="The lab host that ran it.")
    container_name: str = ""
    wall_ms: int = Field(default=0, ge=0)
    teardown_container_absent: bool = False
    teardown_worktree_absent: bool = False
    detail: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _completed_means_clean_evidence(self) -> ModelFocusedTestRunReceipt:
        if self.status is EnumFocusedTestRunStatus.COMPLETED:
            if not self.junit_xml.strip():
                raise ValueError("a completed run must carry its junit XML")
            if not (self.teardown_container_absent and self.teardown_worktree_absent):
                raise ValueError(
                    "a completed run must have passed both teardown checks"
                )
            if self.exit_code is None:
                raise ValueError("a completed run must carry its exit code")
        if len(self.junit_xml.encode("utf-8")) > MAX_JUNIT_BYTES:
            raise ValueError(f"junit_xml exceeds {MAX_JUNIT_BYTES} bytes")
        return self


__all__ = [
    "MAX_JUNIT_BYTES",
    "EnumFocusedTestRunStatus",
    "ModelFocusedTestRunReceipt",
]
