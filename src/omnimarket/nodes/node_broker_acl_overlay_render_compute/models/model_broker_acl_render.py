# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the broker ACL overlay render compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_broker_acl_overlay_render_compute.models.model_broker_acl_overlay import (
    ModelBrokerAclOverlay,
)


class ModelBrokerAclRow(BaseModel):
    """One rendered desired ACL row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str
    resource_type: str
    pattern: str
    name: str
    operation: str


class ModelBrokerAclRenderRequest(BaseModel):
    """The overlay to render into desired ACL rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelBrokerAclOverlay


class ModelBrokerAclRenderResult(BaseModel):
    """Sorted rows, the row count, and the unique sorted principals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    rows: tuple[ModelBrokerAclRow, ...]
    row_count: int = Field(ge=0)
    principals: tuple[str, ...]
