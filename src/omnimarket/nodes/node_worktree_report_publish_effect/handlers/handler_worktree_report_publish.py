# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish completed worktree reports and hand ownership to the landing controller."""

from __future__ import annotations

from typing import Literal

from ..models import ModelWorktreeReportPublishRequest, ModelWorktreeReportPublishResult
from .host import ReportPublishHost, StepError


class HandlerWorktreeReportPublish:
    def __init__(self, host: ReportPublishHost | None = None) -> None:
        self.host = host if host is not None else ReportPublishHost()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelWorktreeReportPublishRequest
    ) -> ModelWorktreeReportPublishResult:
        number, head = 0, ""
        try:
            head, branch, base = self.host.commit_and_push(request)
            number = self.host.open_pull_request(request, head, branch, base)
            self.host.handoff(request, number, head)
        except (StepError, OSError, ValueError) as exc:
            error = (
                exc if isinstance(exc, StepError) else StepError("publish", str(exc))
            )
            recorded, paged = self.host.report_failure(request, error)
            return ModelWorktreeReportPublishResult(
                correlation_id=request.correlation_id,
                outcome="failed",
                pr_number=number,
                head=head,
                step=error.step,
                detail=error.detail,
                friction_recorded=recorded,
                page_delivered=paged,
            )
        return ModelWorktreeReportPublishResult(
            correlation_id=request.correlation_id,
            outcome="handed-off",
            pr_number=number,
            head=head,
        )
