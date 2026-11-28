# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the clean-container customer-path test: the command, one step's reading and the typed result.

Nothing here holds the key or the gateway URL's credentials; a step records its argv template, exit code and a bounded output tail.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumCustomerPathOutcome(StrEnum):
    PASSED = "passed"
    PASSED_STEPS_NOT_RUN = "passed_steps_not_run"
    SETTINGS_ABSENT = "settings_absent"
    KEY_FILE_REFUSED = "key_file_refused"
    CONTAINER_START_FAILED = "container_start_failed"
    STEP_FAILED = "step_failed"


class EnumStepStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ModelCustomerPathCommand(BaseModel):
    """One run. The gateway URL, the key file and the lab probe targets come from the environment, never from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(default="ubuntu:24.04", min_length=1)
    customer_user: str = Field(default="customer", min_length=1)
    source_checkout: str | None = Field(
        default=None,
        description="A checkout of omnimarket copied into the container for the step that needs the unreleased smoke-check node.",
    )
    prompt: str = Field(
        default="Reply with only a python code block holding one pytest test function named test_alive that asserts True. "
        "No prose outside the code block. Automated customer-path test.",
        min_length=1,
    )
    task_type: str = Field(default="test", min_length=1)
    container_ttl_s: int = Field(default=3600, ge=60)


class ModelExecResult(BaseModel):
    """One command run in the container."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ModelStepReading(BaseModel):
    """What one step showed: its declared id, the guide section it follows, its status and a bounded tail of output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    guide_ref: str
    status: EnumStepStatus
    detail: str
    exit_code: int | None = None
    output_tail: str = ""
    guide_gap: str | None = None


class ModelCustomerPathResult(BaseModel):
    """The outcome of one container run: every step's reading, the receipt fields read back and the guide's gaps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumCustomerPathOutcome
    detail: str
    steps: list[ModelStepReading] = Field(default_factory=list)
    receipt_status: str | None = None
    receipt_model: str | None = None
    receipt_provider: str | None = None
    receipt_tests_run: str | None = None
    guide_gaps: list[str] = Field(default_factory=list)
