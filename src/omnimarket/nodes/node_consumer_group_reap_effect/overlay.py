# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The execution overlay of node_consumer_group_reap_effect: every execution setting, none in code.

Found at ``<root>/node_consumer_group_reap_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it. The reap POLICY (the
empty-since bound, the patterns, the per-decision cap) lives in the reducer's overlay, not here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_consumer_group_reap_effect"


class ModelGroupReapOverlay(BaseModel):
    """Execution settings of the consumer group reap effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Whether the effect may issue deletes at all; a dry run never reads it.
    enabled: bool
    #: Whether a run only plans and lists what it would delete, touching nothing.
    dry_run: bool
    #: The most groups one run may delete; a command over it is refused.
    max_deletes_per_run: int = Field(ge=1)
    #: The most groups one delete call issues at once.
    batch_size: int = Field(ge=1)
    #: The broker request timeout in seconds.
    request_timeout_seconds: float = Field(gt=0)
    #: The most read-back listings taken until the deleted groups are gone.
    read_back_attempts: int = Field(ge=1)
    #: The wait between read-back listings, in seconds.
    read_back_wait_seconds: float = Field(ge=0)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelGroupReapOverlay:
    return load_structured_overlay(NODE_NAME, ModelGroupReapOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelGroupReapOverlay",
    "load_overlay",
]
