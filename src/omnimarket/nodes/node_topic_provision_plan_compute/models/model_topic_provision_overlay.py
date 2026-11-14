# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment overlay for the topic provision plan."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
    EnumTopicProvisionMode,
)


class ModelTopicProvisionOverlay(BaseModel):
    """The optional deployment overlay for the provision plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumTopicProvisionMode | None = None
    profile: str | None = None
    lane_manifest: tuple[str, ...] = ()
    contract_roots: tuple[str, ...] = ()
    bootstrap_env: str | None = None
    lane: str | None = (
        None  # a declared lane whose broker and identity the dry run reads
    )
