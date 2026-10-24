# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated database aggregates, before classification."""

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.nodes.node_capture_silence_watch_effect.models.model_actor_newest import (
    ModelActorNewest,
)


class ModelCaptureObservation(BaseModel):
    """Unattributed rows still contribute to the source newest timestamp."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )
    source_newest_at: AwareDatetime | None
    actors: tuple[ModelActorNewest, ...]
