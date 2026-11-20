# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Role and alert-channel settings for the scheduled host unit."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_tailscale_key_expiry_alarm_effect"


class ModelKeyExpiryOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    owner: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    watching_host: str = Field(default="watching-host", pattern=r"^[a-z][a-z0-9-]+$")
    channel_library: Path
    host_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)


def load_overlay() -> ModelKeyExpiryOverlay:
    return load_structured_overlay(NODE_NAME, ModelKeyExpiryOverlay)
