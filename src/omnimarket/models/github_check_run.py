# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub check-run and workflow-run observations, as the webhook ingress node publishes them.

The ingress node (``node_github_webhook_ingress_effect``) owns the producing models. These are the
consuming side's typed view of the same wire payloads, so a payload change fails here at validation
instead of silently dropping a red check.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_state import ISO_Z_PATTERN

# The conclusions a PR-state watcher counts as red. A cancelled check has no verdict.
RED_CHECK_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure", "stale"}
)


class ModelGitHubCheckRunObservation(BaseModel):
    """One completed, non-passing check run on one pull request's head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=3, description="'<owner>/<repo>'.")
    pr_number: int = Field(gt=0)
    head_sha: str = Field(min_length=1)
    base_ref: str | None = None
    check: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    run_id: int | None = Field(default=None, gt=0, description="Actions workflow run.")
    completed_at: str = Field(pattern=ISO_Z_PATTERN)

    @model_validator(mode="after")
    def valid_time(self) -> Self:
        datetime.fromisoformat(self.completed_at)
        return self


class ModelGitHubWorkflowRunObservation(BaseModel):
    """A workflow run's id and name, which no check-run delivery carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=3, description="'<owner>/<repo>'.")
    run_id: int = Field(gt=0)
    workflow: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    status: Literal["requested", "in_progress", "completed"]


def observation_from_wire[T: BaseModel](cls: type[T], value: Mapping[str, object]) -> T:
    """Type a wire payload, ignoring the transport enrichment around it."""
    return cls.model_validate_json(
        json.dumps({k: value[k] for k in cls.model_fields if k in value})
    )
