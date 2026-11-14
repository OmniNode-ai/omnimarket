# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every batching value, read from the node's overlay. There is no default."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDevRollPolicy(BaseModel):
    """The overlay document of node_dev_roll_request_compute.

    The plan's option (b) numbers are the proposed values; the operator has not
    ruled on them (five whys plan, Decisions item 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["shadow", "live"]
    batch_window_seconds: int = Field(gt=0)
    min_start_interval_seconds: int = Field(gt=0)
    healthy_for_seconds: int = Field(gt=0)
    failed_roll_release_after_seconds: int = Field(gt=0)
    known_repositories: tuple[str, ...] = Field(min_length=1)
    build_context_repository: str = Field(min_length=1)
    fallback_git_ref: str = Field(min_length=1)
    scope: Literal["full"]
    runtime_lane: Literal["dev"]
    requested_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _build_context_is_known(self) -> ModelDevRollPolicy:
        if self.build_context_repository not in self.known_repositories:
            raise ValueError("build_context_repository must be a known repository")
        return self
