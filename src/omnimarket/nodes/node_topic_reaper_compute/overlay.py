# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reaper policy, read only from the lane overlay (OMN-21594).

The overlay holds one policy per lane under ``lanes``. A lane with no row, a missing overlay or an
incomplete row has no policy, and the reaper then reports ``REAPER_POLICY_MISSING`` and deletes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_topic_reaper_compute.models.model_topic_reaper import (
    ModelTopicReaperPolicy,
)

NODE_NAME = "node_topic_reaper_compute"


class ModelTopicReaperOverlay(BaseModel):
    """The overlay document: the reaper policy of each lane that has one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lanes: dict[str, ModelTopicReaperPolicy]


def load_reaper_policy(
    lane: str,
    environ: Mapping[str, str] | None = None,
) -> ModelTopicReaperPolicy | None:
    """The policy of ``lane``, or ``None`` when the overlay holds none or an incomplete one.

    ``None`` is the missing-policy answer: the handler then reports ``REAPER_POLICY_MISSING`` and
    deletes nothing. There is no packaged fallback value.
    """
    try:
        overlay = load_structured_overlay(NODE_NAME, ModelTopicReaperOverlay, environ)
    except NodeOverlayError:
        return None
    return overlay.lanes.get(lane)
