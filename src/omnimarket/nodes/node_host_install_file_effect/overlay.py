# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_install_file_effect: every bound of the file steps, none in code.

Found at ``<root>/node_host_install_file_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_host_install_file_effect"


class ModelHostInstallFileOverlay(BaseModel):
    """``digest_algorithm`` is the hashlib algorithm for every digest; ``backup_root`` the absolute directory that holds
    one backup directory per correlation id; ``backup_suffix`` the suffix of each backed-up file; ``manifest_name`` the
    name of the manifest file in the backup directory; ``max_text_bytes`` the longest text a step accepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest_algorithm: str = Field(min_length=1)
    backup_root: str = Field(min_length=1)
    backup_suffix: str = Field(min_length=1)
    manifest_name: str = Field(min_length=1)
    max_text_bytes: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostInstallFileOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostInstallFileOverlay, environ)
