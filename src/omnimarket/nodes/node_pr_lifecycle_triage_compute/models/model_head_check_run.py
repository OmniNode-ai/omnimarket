# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHeadCheckRun: one check-run copy at a PR head, as the check-runs API reports it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_conclusion import (
    CHECK_RUN_PASSING_CONCLUSIONS,
    EnumCheckRunConclusion,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_status import (
    EnumCheckRunStatus,
)


class ModelHeadCheckRun(BaseModel):
    """One check-run copy at the head.

    A head can carry several copies of one check name (a re-run, or one copy
    per caller workflow). Every copy is kept; ``started_at`` and
    ``check_run_id`` order them, so the classifier can take the newest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Check-run name.")
    check_run_id: int = Field(
        ..., ge=1, description="Check-run id (the job id for Actions)."
    )
    status: EnumCheckRunStatus = Field(..., description="Check-run status.")
    conclusion: EnumCheckRunConclusion | None = Field(
        default=None, description="Conclusion; set exactly when status is completed."
    )
    started_at: datetime | None = Field(
        default=None, description="When the copy started."
    )
    completed_at: datetime | None = Field(
        default=None, description="When the copy completed."
    )
    app_slug: str | None = Field(
        default=None, description="GitHub App that owns the check-run."
    )
    run_id: int | None = Field(
        default=None, ge=1, description="Actions workflow run id, when an Actions job."
    )
    run_attempt: int | None = Field(
        default=None,
        ge=1,
        description="Actions run attempt; read from the jobs API for non-green copies.",
    )
    failed_step: str | None = Field(
        default=None,
        description="First failed (else cancelled or timed-out) step of a non-green job.",
    )
    required: bool = Field(
        ..., description="Whether the base branch requires this context."
    )
    run_event: str | None = Field(
        default=None,
        description="Event that started the Actions run (pull_request, push, ...), when read.",
    )
    workflow_path: str | None = Field(
        default=None, description="Caller workflow file of the run, when read."
    )
    caller_changed_on_base: bool = Field(
        default=False,
        description=(
            "Whether workflow_path changed on the base between the head's "
            "merge base and the base tip when the head was read."
        ),
    )
    annotations: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "First line of each failure-level annotation on a non-green copy "
            "(for example a concurrency cancel, a time limit or an API refusal)."
        ),
    )
    named_blockers: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "For an aggregate check such as CI Summary: the checks its own final "
            "report names as failed, missing or pending. Empty otherwise."
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> ModelHeadCheckRun:
        completed = self.status is EnumCheckRunStatus.COMPLETED
        if completed and self.conclusion is None:
            raise ValueError(f"{self.name}: a completed check-run needs a conclusion")
        if not completed and self.conclusion is not None:
            raise ValueError(
                f"{self.name}: only a completed check-run carries a conclusion"
            )
        if self.failed_step is not None and (
            self.conclusion is None or self.conclusion in CHECK_RUN_PASSING_CONCLUSIONS
        ):
            raise ValueError(f"{self.name}: failed_step is set on a non-failed copy")
        not_failed = (
            self.conclusion is None or self.conclusion in CHECK_RUN_PASSING_CONCLUSIONS
        )
        if not_failed and (self.annotations or self.named_blockers):
            raise ValueError(
                f"{self.name}: annotations and named_blockers belong to a non-green copy"
            )
        if self.caller_changed_on_base and not self.workflow_path:
            raise ValueError(
                f"{self.name}: caller_changed_on_base needs the workflow_path it is about"
            )
        return self


__all__: list[str] = ["ModelHeadCheckRun"]
