# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_install_state_reducer: every bound of the fold, none in code.

Found at ``<root>/node_host_install_state_reducer/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_host_install_state_reducer"


class ModelHostInstallStateOverlay(BaseModel):
    """``digest_hex_length`` is the length of every digest the fold accepts; ``history_limit`` the number of folded steps
    the record keeps (the newest); ``detail_max_chars`` the longest detail text the record keeps (longer is cut)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest_hex_length: int = Field(ge=1)
    history_limit: int = Field(ge=1)
    detail_max_chars: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostInstallStateOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostInstallStateOverlay, environ)
