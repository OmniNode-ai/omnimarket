# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_install_readback_compute: the digest algorithm, none in code.

Found at ``<root>/node_host_install_readback_compute/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_host_install_readback_compute"


class ModelHostInstallReadbackOverlay(BaseModel):
    """``digest_algorithm`` is the hashlib algorithm name used to compute both digests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest_algorithm: str = Field(min_length=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostInstallReadbackOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostInstallReadbackOverlay, environ)
