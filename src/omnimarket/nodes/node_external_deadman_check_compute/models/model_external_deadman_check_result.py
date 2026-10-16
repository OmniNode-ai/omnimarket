# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the external dead-man check (OMN-20808)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .enum_external_deadman_action import EnumExternalDeadmanAction
from .model_external_deadman_reason_finding import ModelExternalDeadmanReasonFinding


class ModelExternalDeadmanCheckResult(BaseModel):
    """What the workflow does: raise or stay quiet, which issue, and whether to post to Slack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    reasons: tuple[ModelExternalDeadmanReasonFinding, ...]
    action: EnumExternalDeadmanAction
    #: The open issue to refresh; ``None`` when the action is to open one or nothing.
    issue_number: int | None
    issue_title: str
    issue_body: str
    #: True when the Slack post goes out: a newly opened issue, or an update whose reasons changed.
    notify: bool
    slack_text: str
