# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_consumer_group_state_reducer: every bound of the fold, none in code.

Found at ``<root>/node_consumer_group_state_reducer/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_consumer_group_state_reducer"


class ModelGroupStateOverlay(BaseModel):
    """Overlay configuration for the consumer group state reducer.

    ``empty_since_bound_seconds``: a group must be EMPTY for at least this many seconds before it can be reaped.
    ``max_reap_per_decision``: at most this many groups are reaped in one decision (first by name order).
    ``reapable_patterns``: regex patterns a group name must match to be reaped (at least one).
    ``protected_patterns``: regex patterns a group name must NOT match to be reaped (protected wins).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    empty_since_bound_seconds: int = Field(ge=0)
    max_reap_per_decision: int = Field(ge=1)
    reapable_patterns: tuple[str, ...] = Field(min_length=1)
    protected_patterns: tuple[str, ...]

    @field_validator("reapable_patterns", "protected_patterns", mode="before")
    @classmethod
    def _validate_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError("a pattern does not compile") from None
        return value


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelGroupStateOverlay:
    return load_structured_overlay(NODE_NAME, ModelGroupStateOverlay, environ)
