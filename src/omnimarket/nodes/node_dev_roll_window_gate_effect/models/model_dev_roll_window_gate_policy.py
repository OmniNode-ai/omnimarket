# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of node_dev_roll_window_gate_effect.

Every value is an overlay value: the window, the minimum start interval, the health
bounds, the repositories, the requester pattern, the bound on held commands and the
store. There is no packaged fallback.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollPolicy


class ModelDevRollGateInMemoryStore(BaseModel):
    """The in-memory gate store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory"]


class ModelDevRollGateFileStore(BaseModel):
    """The file gate store configuration: a directory under an environment-named home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["file"]
    home_env: str = Field(min_length=1)
    relpath: str = Field(min_length=1)


class ModelDevRollWindowGatePolicy(BaseModel):
    """The overlay document of node_dev_roll_window_gate_effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["live", "off"]
    issuer_repository_pattern: str = Field(min_length=1)
    batch_window_seconds: int = Field(gt=0)
    min_start_interval_seconds: int = Field(gt=0)
    healthy_for_seconds: int = Field(gt=0)
    failed_roll_release_after_seconds: int = Field(gt=0)
    known_repositories: tuple[str, ...] = Field(min_length=1)
    build_context_repository: str = Field(min_length=1)
    fallback_git_ref: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    held_kept: int = Field(gt=0)
    store: Annotated[
        ModelDevRollGateInMemoryStore | ModelDevRollGateFileStore,
        Field(discriminator="backend"),
    ]

    @model_validator(mode="after")
    def _consistent(self) -> ModelDevRollWindowGatePolicy:
        try:
            compiled = re.compile(self.issuer_repository_pattern)
        except re.error as exc:
            raise ValueError(
                f"issuer_repository_pattern is not a regular expression ({exc})"
            ) from exc
        if "repository" not in compiled.groupindex:
            raise ValueError(
                "issuer_repository_pattern must carry a named group 'repository'"
            )
        if self.build_context_repository not in self.known_repositories:
            raise ValueError("build_context_repository must be a known repository")
        return self

    def to_roll_policy(self) -> ModelDevRollPolicy:
        """The batching node's policy for the decision this gate asks of it."""
        return ModelDevRollPolicy(
            mode="live",
            batch_window_seconds=self.batch_window_seconds,
            min_start_interval_seconds=self.min_start_interval_seconds,
            healthy_for_seconds=self.healthy_for_seconds,
            failed_roll_release_after_seconds=self.failed_roll_release_after_seconds,
            known_repositories=self.known_repositories,
            build_context_repository=self.build_context_repository,
            fallback_git_ref=self.fallback_git_ref,
            scope="full",
            runtime_lane="dev",
            requested_by=self.requested_by,
        )
