# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every bound of the running-revision compute, read from the node's overlay."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class ModelRunningRevisionPolicy(BaseModel):
    """The overlay document of node_dev_running_revision_compute.

    No packaged default: an absent overlay refuses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    known_repositories: tuple[str, ...] = Field(min_length=1)
    behind_threshold_seconds: int = Field(gt=0)
    median_merge_to_running_bound_seconds: int = Field(gt=0)
    event_bus: ModelNodeEventBus | None = None

    @model_validator(mode="after")
    def _repositories_unique(self) -> ModelRunningRevisionPolicy:
        if len(set(self.known_repositories)) != len(self.known_repositories):
            raise ValueError("known_repositories must be unique")
        return self
