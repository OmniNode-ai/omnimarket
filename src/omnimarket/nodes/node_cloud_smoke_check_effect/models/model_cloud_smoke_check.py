# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the cloud smoke check: the command, one gateway answer and the typed result.

The result names a status word, a workflow id and the answering model; it never holds the key or the gateway URL's credentials.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumCloudSmokeOutcome(StrEnum):
    PASSED = "passed"
    SETTINGS_ABSENT = "settings_absent"
    KEY_FILE_REFUSED = "key_file_refused"
    GATEWAY_UNREACHABLE = "gateway_unreachable"
    GATEWAY_UNAVAILABLE = "gateway_unavailable"
    AUTH_REFUSED = "auth_refused"
    SUBMIT_REFUSED = "submit_refused"
    RUN_FAILED = "run_failed"
    RUN_TIMED_OUT = "run_timed_out"
    RECEIPT_UNREADABLE = "receipt_unreadable"
    RECEIPT_NAMES_NO_MODEL = "receipt_names_no_model"


class ModelCloudSmokeCommand(BaseModel):
    """One smoke run. The gateway URL and the key file come from the environment, never from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(
        default="Reply with only a python code block holding one pytest test function named test_alive that asserts True. "
        "No prose outside the code block. Automated smoke check.",
        min_length=1,
    )
    task_type: str = Field(default="test", min_length=1)
    max_tokens: int = Field(default=512, ge=1)
    run_budget_s: float = Field(default=120.0, gt=0)
    poll_interval_s: float = Field(default=2.0, gt=0)


class ModelGatewayAnswer(BaseModel):
    """What one gateway request showed; ``status`` is None, with ``error`` set, when no HTTP answer arrived."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int | None
    body: Any = None
    error: str | None = None


class ModelCloudSmokeResult(BaseModel):
    """The outcome of one smoke run, with the answering model when the run passed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumCloudSmokeOutcome
    detail: str
    workflow_id: str | None = None
    model: str | None = None
    provider: str | None = None
    route: str | None = None
